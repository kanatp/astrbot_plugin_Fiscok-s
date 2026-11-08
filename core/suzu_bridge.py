"""
小铃（Suzu）桌面端桥接。

在 AstrBot 插件内起一个独立的、Bearer Token 保护的 HTTP 服务，给桌面端提供：
- 人设基线 / 演化自我设定 / 情绪 / 好感度 / 个人记忆的读取
- 以 AstrBot 事件管线完成桌面端聊天（默认），也可选插件直连 Provider
- 桌面端笔记写入个人长期记忆
- 手动触发一次人设演化
- 按 v2 桌面协议返回结构化动作与动作回执

只依赖 aiohttp（AstrBot 自带），不改动 QQ 侧任何行为。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import re
import time
from collections import OrderedDict
from datetime import datetime
from ipaddress import ip_address, ip_network
from typing import Any, Dict, List, Optional

from aiohttp import web

from astrbot.api import logger
from astrbot.api.message_components import Image

from .api.pipeline_chat_apis import chat_via_astrbot
from .api.persona_evolution_apis import evolve_persona
from .api.personal_memory_apis import retrieve_memories
from .api.provider_utils import resolve_provider
from .api.topic_memory_apis import get_text_embedding, resolve_embedding_provider
from .suzu_settings import Live2DSettings
from .prompts import (
    format_emotion_state_injection,
    format_persona_overlay_injection,
    format_personal_memory_injection,
)

PROTOCOL_VERSION = 2
BRIDGE_VERSION = 2
BRIDGE_PLUGIN = "astrbot_plugin_Fiscok"
MAX_HISTORY = 12
MAX_TEXT = 8000
MAX_IMAGE_BASE64 = 7_000_000
RECEIPT_TTL = 300.0
RECEIPT_MAX = 64
CHAT_TIMEOUT = 60.0
PIPELINE_FIRST_TIMEOUT = 65.0

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_/-]{1,100}$")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _error(message: str, status: int = 400, code: str = "") -> web.Response:
    body: Dict[str, Any] = {"ok": False, "error": message}
    if code:
        body["code"] = code
    return _json(body, status=status)


def _iso_now() -> str:
    return datetime.now().isoformat()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if limit > 0 else text


async def _read_json(request: web.Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_ip(raw: Any) -> str:
    """把 IPv4-mapped IPv6（::ffff:1.2.3.4）和带 zone 的链路本地地址归一化。"""
    text = str(raw or "").strip()
    if text.startswith("::ffff:"):
        text = text[7:]
    return text.split("%", 1)[0]


def _parse_networks(values: Any) -> List[Any]:
    networks: List[Any] = []
    if not isinstance(values, list):
        return networks
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            networks.append(ip_network(text, strict=False))
        except ValueError:
            logger.warning(f"[Fiscok's][suzu] 忽略无效的 IP/网段配置: {text}")
    return networks


class SuzuBridge:
    """桌面端桥接服务；由插件实例持有，插件卸载时停止。"""

    def __init__(self, plugin: Any, config: Dict[str, Any]):
        self.plugin = plugin
        self.config = config or {}
        # 监听地址与端口是启动前必须确定的引导配置，其余 Live2D 设置在 WebUI 里维护。
        self.host = str(self.config.get("host") or "127.0.0.1").strip()
        self.port = int(self.config.get("port") or 6199)
        self.settings = Live2DSettings(plugin)
        self.apply_settings(self.settings.data)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._started = False
        self._evolve_lock = asyncio.Lock()
        # 供插件 WebUI 展示桌面端最近一次连接与同步行为。
        self.last_client: Dict[str, Any] = {}
        self.last_bootstrap_at: str = ""
        self.last_chat_at: str = ""
        self.request_count = 0
        self.reject_count = 0
        # v2 协议：同会话串行、requestId 去重、动作回执（均为内存驻留）
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._recent_requests: "OrderedDict[str, float]" = OrderedDict()
        self._action_records: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        # 插件 WebUI（密码会话）；初始化失败时降级为纯桥接，不影响桌面端使用。
        self.webui = None
        try:
            from .suzu_webui import SuzuWebUI

            self.webui = SuzuWebUI(self)
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 插件 WebUI 初始化失败，仅启用桥接: {exc}")

    # ---------------- 运行期设置 ----------------

    def apply_settings(self, data: Dict[str, Any]) -> None:
        """把 WebUI 保存的设置应用到运行中的桥接（令牌/白名单立即生效）"""
        self.token = str(data.get("token") or "").strip()
        self.default_user_id = str(data.get("default_user_id") or "desktop_suzu").strip()
        self.memory_qq = str(data.get("memory_qq") or "").strip()
        self.persona_id = str(data.get("persona_id") or "").strip()
        self.default_nickname = str(data.get("default_nickname") or "小主人").strip()
        self.llm_provider_id = str(data.get("llm_provider_id") or "").strip()
        # astrbot：把消息注入 AstrBot 事件队列，复用主 Agent 管线；direct：插件直连 Provider。
        self.chat_mode = "direct" if data.get("chat_mode") == "direct" else "astrbot"
        self.vision_input = bool(data.get("vision_input", True))
        self.allowed_networks = _parse_networks(data.get("allowed_ips") or [])
        self.trusted_networks = _parse_networks(
            data.get("trusted_proxies") or ["127.0.0.1", "::1"]
        )

    @property
    def local_only(self) -> bool:
        """没有令牌也没有白名单时，/suzu 接口只接受本机请求"""
        return not self.token and not self.allowed_networks

    def save_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验并保存 WebUI 提交的设置，返回生效后的设置"""
        data = self.settings.save(payload)
        self.apply_settings(data)
        whitelist = f"{len(self.allowed_networks)} 条" if self.allowed_networks else "未限制"
        logger.info(
            f"[Fiscok's][suzu] Live2D 设置已更新 "
            f"(token={'已配置' if self.token else '未配置'}, 白名单={whitelist})"
        )
        return data

    def settings_view(self) -> Dict[str, Any]:
        """供 WebUI 展示的 Live2D 设置与服务状态"""
        data = dict(self.settings.data)
        data["expression_map"] = list(data.get("expression_map") or [])
        try:
            data["personas"] = [
                str(item.get("name") or "")
                for item in self.plugin.context.persona_manager.personas_v3
                if item.get("name")
            ]
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 读取人格列表失败: {exc}")
            data["personas"] = []
        data.update(
            {
                "host": self.host,
                "port": self.port,
                "started": self._started,
                "localOnly": self.local_only,
            }
        )
        return data

    async def restart(self) -> Dict[str, Any]:
        """按当前插件配置重新加载监听地址与端口"""
        old_host, old_port = self.host, self.port
        await self.stop()
        section = self.plugin.config.get("suzu_bridge_config", {}) or {}
        self.host = str(section.get("host") or "127.0.0.1").strip()
        try:
            self.port = int(section.get("port") or 6199)
        except (TypeError, ValueError):
            self.port = 6199
        await self.start()
        if not self._started and (self.host, self.port) != (old_host, old_port):
            # 新地址启动失败时回退到原地址，避免 WebUI 直接失联。
            logger.warning(
                f"[Fiscok's][suzu] 新地址 {self.host}:{self.port} 启动失败，"
                f"回退到 {old_host}:{old_port}"
            )
            self.host, self.port = old_host, old_port
            await self.start()
        return {"ok": self._started, "host": self.host, "port": self.port}

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        if self._started:
            return
        if not self.token and not self.allowed_networks:
            # WebUI 有独立的密码会话，可远程访问；但 /suzu 接口只接受本机请求，
            # 由鉴权中间件拦截，避免默认暴露聊天接口。
            logger.warning(
                "[Fiscok's][suzu] 未配置访问令牌与 IP 白名单，"
                "桌面桥接 /suzu 接口只接受本机请求；WebUI 仍可正常访问"
            )
        elif not self.token:
            logger.warning("[Fiscok's][suzu] 未配置访问令牌，仅靠 IP 白名单防护")

        app = web.Application(
            middlewares=[self._auth_middleware],
            # 桌面端截图以 base64 上传，默认 1MiB 不够
            client_max_size=9 * 1024 * 1024,
        )
        app.router.add_get("/suzu/health", self._health)
        app.router.add_get("/suzu/bootstrap", self._bootstrap)
        app.router.add_post("/suzu/chat", self._chat)
        app.router.add_post("/suzu/action-result", self._action_result)
        app.router.add_get("/suzu/memory", self._memory)
        app.router.add_post("/suzu/memory/note", self._memory_note)
        app.router.add_post("/suzu/persona/evolve", self._persona_evolve)
        if self.webui is not None:
            self.webui.register(app)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port, shutdown_timeout=1)
        logger.info(f"[Fiscok's][suzu] 正在监听 {self.host}:{self.port} ...")
        try:
            await self._site.start()
        except PermissionError as exc:
            logger.error(
                f"[Fiscok's][suzu] 端口 {self.port} 绑定失败（无权限）：{exc}。"
                "Linux/Unix 下低于 1024 的端口需要 root 或 CAP_NET_BIND_SERVICE，"
                "建议改用 1024 以上的端口；Docker 部署还需要在容器映射该端口。"
            )
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            return
        except OSError as exc:
            errno = getattr(exc, "errno", None)
            if errno in (48, 98, 10048):
                reason = "端口已被其他程序占用"
            elif errno in (99, 10049):
                reason = "监听地址不可用（该地址不属于本机网卡）"
            else:
                reason = "端口无法绑定"
            logger.error(
                f"[Fiscok's][suzu] 端口 {self.port} 启动失败（{reason}）：{exc}。"
                "请修改 suzu_bridge_config.port 或关闭占用该端口的程序；"
                "若使用 Docker，请确认宿主机端口已映射到容器内同一端口。"
            )
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            return
        self._started = True
        whitelist = f"{len(self.allowed_networks)} 条" if self.allowed_networks else "未限制"
        logger.info(
            f"[Fiscok's][suzu] 桌面桥接已启动: http://{self.host}:{self.port}/suzu "
            f"(token={'已配置' if self.token else '未配置'}, 白名单={whitelist})"
        )
        logger.info(f"[Fiscok's][suzu] WebUI 地址: http://{self.host}:{self.port}/ "
                    f"(密码见本机日志或插件数据目录 suzu_webui.json)")
        if self.host == "0.0.0.0":
            logger.info(
                "[Fiscok's][suzu] 已监听所有网卡；公网能否访问还取决于云安全组、"
                "Docker 端口映射与前置反向代理（502 通常是代理连不上该端口）"
            )

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        logger.info("[Fiscok's][suzu] 桌面桥接已停止")

    # ---------------- 鉴权 ----------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # 白名单校验的是「谁直连到 Bridge」：反代部署时这里应是代理自身地址。
        peer = self._peer_ip(request)
        if self.allowed_networks and not self._in_networks(peer, self.allowed_networks):
            self.reject_count += 1
            client = self._client_ip(request)
            logger.warning(
                f"[Fiscok's][suzu] 拒绝 {client or '未知地址'}（直连 {peer or '未知'}）"
                f" 访问 {request.path}：代理地址不在白名单"
            )
            return _error("来源地址不在白名单", 403)
        if request.method == "OPTIONS":
            return web.Response(status=204)
        # 未配置令牌与白名单时，桌面桥接接口只接受本机请求；WebUI 有自己的密码会话，不受影响。
        # 这里用 _client_ip：经可信反代访问时会取 X-Forwarded-For，避免把代理的 127.0.0.1 当成客户端。
        if (
            self.local_only
            and request.path.startswith("/suzu/")
            and request.path != "/suzu/health"
            and not self._is_loopback(self._client_ip(request))
        ):
            self.reject_count += 1
            logger.warning(
                f"[Fiscok's][suzu] 拒绝 {self._client_ip(request) or '未知地址'} 访问 "
                f"{request.path}：未配置访问令牌，仅允许本机调用"
            )
            return _error("未配置访问令牌，桌面桥接仅允许本机访问", 403)
        # 插件 WebUI 使用独立的密码会话，不走桌面端令牌。
        if request.path == "/" or request.path.startswith("/ui/"):
            return await handler(request)
        if request.path == "/suzu/health":
            return await handler(request)
        if not self.token:
            # 无令牌模式已由上面的本机请求限制保护。
            return await handler(request)
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = request.headers.get("X-Suzu-Token", "")
        if not hmac.compare_digest(supplied, self.token):
            self.reject_count += 1
            return _error("访问令牌不正确", 401)
        self.request_count += 1
        if request.path.startswith("/suzu/"):
            self.last_client = {
                "at": _iso_now(),
                "ip": self._client_ip(request),
                "peer": peer,
                "path": request.path,
                "method": request.method,
                "userAgent": _clip(request.headers.get("User-Agent"), 120),
            }
        return await handler(request)

    # ---------------- 来源地址 ----------------

    def _peer_ip(self, request: web.Request) -> str:
        peer = ""
        transport = request.transport
        if transport is not None:
            info = transport.get_extra_info("peername")
            if info:
                peer = info[0]
        return _normalize_ip(peer or request.remote or "")

    def _client_ip(self, request: web.Request) -> str:
        """只有可信代理传来的 X-Forwarded-For 才采信，避免客户端伪造来源。"""
        peer = self._peer_ip(request)
        if not self._in_networks(peer, self.trusted_networks):
            return peer
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return _normalize_ip(first)
        real = request.headers.get("X-Real-IP", "")
        return _normalize_ip(real) if real.strip() else peer

    @staticmethod
    def _in_networks(ip_text: str, networks: List[Any]) -> bool:
        if not networks or not ip_text:
            return False
        try:
            address = ip_address(ip_text)
        except ValueError:
            return False
        return any(address in network for network in networks)

    @staticmethod
    def _is_loopback(ip_text: str) -> bool:
        if not ip_text:
            return False
        if ip_text in ("localhost", "::1"):
            return True
        try:
            return ip_address(ip_text).is_loopback
        except ValueError:
            return False

    # ---------------- 数据辅助 ----------------

    def _user_id(self, supplied: Any = "") -> str:
        if self.memory_qq:
            return self.memory_qq
        return _clip(supplied, 80) or self.default_user_id

    @property
    def memory_user_id(self) -> str:
        """个人记忆与好感度归属：绑定 QQ 时直接复用该账号，否则用桌面身份。"""
        return self.memory_qq or self.default_user_id

    def _nickname(self, supplied: Any = "") -> str:
        return _clip(supplied, 40) or self.default_nickname

    def _umo(self, user_id: str) -> str:
        return f"desktop:{user_id}"

    def _qq_platform(self):
        """找到 QQ（OneBot/aiocqhttp）平台实例，找不到返回 None"""
        try:
            for inst in self.plugin.context.platform_manager.get_insts():
                meta = inst.meta()
                pid = str(getattr(meta, "id", "") or "")
                name = str(getattr(meta, "name", "") or "")
                if not pid:
                    continue
                if (
                    name == "aiocqhttp"
                    or "qq" in name.lower()
                    or "qq" in pid.lower()
                    or "napcat" in pid.lower()
                ):
                    return inst
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 解析 QQ 平台失败: {exc}")
        return None

    def _qq_private_umo(self) -> str:
        """绑定 QQ 后复用该账号真实私聊会话的 umo，使演化对话与 QQ 共用一份。"""
        if not self.memory_qq:
            return ""
        inst = self._qq_platform()
        if inst is None:
            return ""
        return f"{inst.meta().id}:FriendMessage:{self.memory_qq}"

    @staticmethod
    def _bot_self_id(inst: Any) -> str:
        """尽量取到平台实例的机器人账号，取不到时用 "0"（仅用于过滤自己发的消息）"""
        for holder in (inst, getattr(inst, "bot", None), getattr(inst, "client", None)):
            if holder is None:
                continue
            value = getattr(holder, "self_id", None)
            if value:
                return str(value)
        return "0"

    def _pipeline_target(self) -> tuple:
        """返回注入事件用的 (平台实例 id, 平台显示名, 机器人账号)。

        绑定 QQ 时用真实 QQ 平台实例 id，使 umo 与 QQ 私聊完全一致；
        平台显示名固定为 suzu_desktop，避免被当作 QQ 消息做合并转发等额外装饰。
        """
        if not self.memory_qq:
            return "suzu_desktop", "suzu_desktop", "0"
        inst = self._qq_platform()
        if inst is None:
            logger.warning(
                "[Fiscok's][suzu] 未找到 QQ 平台实例，桌面聊天回退到 AstrBot 默认配置"
            )
            return "suzu_desktop", "suzu_desktop", "0"
        return str(inst.meta().id), "suzu_desktop", self._bot_self_id(inst)

    def _evolution_umo(self, user_id: str, umo: str) -> str:
        return self._qq_private_umo() or umo

    def _persona_umos(self, user_id: str) -> List[tuple]:
        """基线只用 AstrBot 默认/指定人格，不把 QQ 会话人格当作基线。"""
        return [("", self._umo(user_id))]

    @staticmethod
    def _persona_fields(persona: Any) -> Dict[str, str]:
        if isinstance(persona, dict):
            name = persona.get("name") or ""
            prompt = persona.get("prompt") or persona.get("system_prompt") or ""
        else:
            name = getattr(persona, "name", "") or ""
            prompt = getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "") or ""
        return {"name": _clip(name, 40), "prompt": _clip(prompt, 4000)}

    async def _baseline_persona(self, user_id: str, umo: str) -> Dict[str, str]:
        if self.persona_id:
            try:
                persona = self.plugin.context.persona_manager.get_persona_v3_by_id(self.persona_id)
                fields = self._persona_fields(persona)
                if fields["prompt"]:
                    return fields
            except Exception as exc:
                logger.warning(f"[Fiscok's][suzu] 读取指定人格 {self.persona_id} 失败: {exc}")
        for platform_id, candidate in self._persona_umos(user_id):
            try:
                if platform_id:
                    _, persona, _, _ = await self.plugin.context.persona_manager.resolve_selected_persona(
                        umo=candidate,
                        conversation_persona_id=None,
                        platform_name=platform_id,
                        provider_settings=self.plugin.config.get("provider_settings"),
                    )
                else:
                    persona = await self.plugin.context.persona_manager.get_default_persona_v3(candidate)
                fields = self._persona_fields(persona)
                if fields["prompt"]:
                    return fields
            except Exception as exc:
                logger.warning(f"[Fiscok's][suzu] 读取 persona 失败（{candidate}）: {exc}")
        return {"name": "", "prompt": ""}

    def _personal_memories(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            return list(self.plugin.data_manager.get_personal_memories(user_id) or [])
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 读取个人记忆失败: {exc}")
            return []

    def _memory_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(item.get("id", "")),
            "category": str(item.get("category", "")),
            "text": _clip(item.get("text"), 500),
            "importance": int(item.get("importance", 3) or 3),
            "createdAt": str(item.get("created_at", "")),
            "updatedAt": str(item.get("updated_at", "")),
        }

    async def _match_memories(
        self, user_id: str, query: str, limit: int = 4
    ) -> List[Dict[str, Any]]:
        """优先向量检索，Embedding 不可用时退回按重要度/时间取最近几条。"""
        memories = self._personal_memories(user_id)
        if not memories:
            return []
        cfg = self.plugin.config.get("personal_memory_config", {}) or {}
        if cfg.get("available", False) and query:
            provider_id = str(cfg.get("embedding_provider_id", "") or "")
            if resolve_embedding_provider(self.plugin.context, provider_id) is not None:
                vector = await get_text_embedding(self.plugin.context, provider_id, query)
                if vector:
                    matched = retrieve_memories(
                        memories,
                        vector,
                        top_k=limit,
                        min_similarity=float(cfg.get("min_similarity", 0.45) or 0.45),
                    )
                    if matched:
                        return matched
        memories.sort(
            key=lambda item: (
                int(item.get("importance", 3) or 3),
                str(item.get("updated_at") or item.get("created_at") or ""),
            ),
            reverse=True,
        )
        return memories[:limit]

    # ---------------- v2 协议辅助 ----------------

    def _scope_key(self, session_id: str) -> str:
        """认证身份 + 会话；没有 sessionId 的旧客户端按身份整体串行"""
        return f"{self.memory_user_id}/{session_id or 'default'}"

    def _session_lock(self, scope: str) -> asyncio.Lock:
        lock = self._session_locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[scope] = lock
        return lock

    def _prune_requests(self, now: float) -> None:
        expired = [
            key
            for key, at in self._recent_requests.items()
            if now - float(at) > RECEIPT_TTL
        ]
        for key in expired:
            self._recent_requests.pop(key, None)

    def _seen_request(self, scope: str, request_id: str) -> bool:
        """首次出现返回 False 并记录；五分钟内重复出现返回 True"""
        now = time.monotonic()
        self._prune_requests(now)
        key = f"{scope}|{request_id}"
        if key in self._recent_requests:
            return True
        self._recent_requests[key] = now
        while len(self._recent_requests) > RECEIPT_MAX:
            self._recent_requests.popitem(last=False)
        return False

    def _prune_action_records(self, now: float) -> None:
        expired = [
            key
            for key, record in self._action_records.items()
            if now - float(record.get("at", 0)) > RECEIPT_TTL
        ]
        for key in expired:
            self._action_records.pop(key, None)

    def _remember_actions(
        self, scope: str, request_id: str, actions: List[Dict[str, Any]]
    ) -> None:
        """保存本轮下发的动作，供 /suzu/action-result 校验回执"""
        now = time.monotonic()
        self._prune_action_records(now)
        self._action_records[f"{scope}|{request_id}"] = {
            "at": now,
            "actions": [dict(item) for item in actions],
            "reported": False,
        }
        while len(self._action_records) > RECEIPT_MAX:
            self._action_records.popitem(last=False)

    def _action_record(self, scope: str, request_id: str) -> Optional[Dict[str, Any]]:
        key = f"{scope}|{request_id}"
        record = self._action_records.get(key)
        if record is None:
            return None
        if time.monotonic() - float(record.get("at", 0)) > RECEIPT_TTL:
            self._action_records.pop(key, None)
            return None
        return record

    def _environment_summary(self, environment: Any) -> str:
        """把桌面环境整理成一段可注入系统提示词的简要描述"""
        if not isinstance(environment, dict):
            return ""
        parts: List[str] = []
        summary = _clip(environment.get("summary"), 300)
        if summary:
            parts.append(summary)
        window = environment.get("window")
        if isinstance(window, dict):
            app = _clip(window.get("app"), 80)
            title = _clip(window.get("title"), 120)
            if app or title:
                parts.append(f"当前窗口：{app}{(' · ' + title) if title else ''}")
        text = _clip(environment.get("text"), 400)
        if text:
            parts.append(f"窗口文字：{text}")
        return "\n".join(parts)

    def _parse_image(self, environment: Any) -> tuple:
        """返回 (base64 字符串, 警告列表)；未开启图片输入或图片不合法时返回空串"""
        warnings: List[str] = []
        if not isinstance(environment, dict):
            return "", warnings
        image = environment.get("image")
        if not isinstance(image, dict):
            return "", warnings
        if not self.vision_input:
            warnings.append("桥接已关闭图片输入，本轮忽略桌面截图")
            return "", warnings
        mime = str(image.get("mimeType") or "image/jpeg").strip().lower()
        if mime not in ("image/jpeg", "image/png"):
            warnings.append(f"不支持的图片类型 {mime}，已忽略")
            return "", warnings
        data = image.get("dataBase64")
        if not isinstance(data, str) or not data.strip():
            warnings.append("图片缺少 dataBase64，已忽略")
            return "", warnings
        data = data.strip()
        if len(data) > MAX_IMAGE_BASE64:
            warnings.append("图片超过 7MB 上限，已忽略")
            return "", warnings
        try:
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            warnings.append("图片 base64 不合法，已忽略")
            return "", warnings
        return data, warnings

    @staticmethod
    def _provider_supports_image(provider: Any) -> bool:
        try:
            modalities = provider.provider_config.get("modalities", None)
        except Exception:
            return False
        if modalities == []:
            # 迁移遗留的空列表按“未配置”处理，视为支持
            return True
        return isinstance(modalities, list) and "image" in modalities

    def _vision_provider_available(self) -> bool:
        """是否真的接通了支持图片的 Provider（用于 health.features.visionInput）"""
        if not self.vision_input:
            return False
        try:
            providers = list(self.plugin.context.get_all_providers() or [])
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 读取 Provider 列表失败: {exc}")
            return False
        return any(self._provider_supports_image(item) for item in providers)

    def _success_response(
        self,
        *,
        request_id: str,
        scope: str,
        reply: str,
        user_id: str,
        nickname: str,
        vision_used: bool,
        warnings: List[str],
        capabilities: Dict[str, Any],
    ) -> web.Response:
        """统一构造 v2 成功响应，同时保留旧版 live2d 字段"""
        reply = _clip(reply, 12000)
        emotion = str(self.plugin.current_emotion or "")
        affinity = self.plugin.data_manager.get_user_affinity(user_id, nickname)
        live2d = self.settings.build_live2d_control(emotion, affinity, reply)
        actions = self.settings.build_actions(
            capabilities,
            str(live2d.get("expression", "")),
            str(live2d.get("motion", "")),
        )
        if request_id:
            self._remember_actions(scope, request_id, actions)
        return _json(
            {
                "ok": True,
                "protocolVersion": PROTOCOL_VERSION,
                "requestId": request_id,
                "reply": reply,
                "emotion": emotion,
                "affinity": affinity,
                "visionUsed": bool(vision_used),
                "actions": actions,
                "warnings": list(warnings),
                "live2d": live2d,
            }
        )

    # ---------------- 路由 ----------------

    async def _health(self, request: web.Request) -> web.Response:
        return _json(
            {
                "ok": True,
                "bridge": "suzu",
                "plugin": BRIDGE_PLUGIN,
                "version": BRIDGE_VERSION,
                "protocolVersion": PROTOCOL_VERSION,
                "features": {
                    "visionInput": self._vision_provider_available(),
                    "structuredActions": True,
                    "actionReceipts": True,
                },
                "time": _iso_now(),
            }
        )

    async def _bootstrap(self, request: web.Request) -> web.Response:
        self.last_bootstrap_at = _iso_now()
        user_id = self._user_id(request.query.get("user_id"))
        nickname = self._nickname(request.query.get("nickname"))
        umo = self._umo(user_id)
        baseline = await self._baseline_persona(user_id, umo)
        evolved = self.plugin.data_manager.get_evolved_persona() or {}
        affinity = self.plugin.data_manager.get_user_affinity(user_id, nickname)
        memories = self._personal_memories(user_id)
        memories.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        features = {
            "personaEvolution": bool(
                (self.plugin.config.get("persona_evolution_config", {}) or {}).get(
                    "available", False
                )
            ),
            "emotion": bool(
                (self.plugin.config.get("emotion_config", {}) or {}).get("available", False)
            ),
            "personalMemory": bool(
                (self.plugin.config.get("personal_memory_config", {}) or {}).get(
                    "available", False
                )
            ),
        }
        return _json(
            {
                "ok": True,
                "now": _iso_now(),
                "user": {
                    "id": user_id,
                    "boundQQ": self.memory_qq,
                    "nickname": nickname,
                    "affinity": affinity,
                },
                "emotion": str(self.plugin.current_emotion or ""),
                "persona": {
                    "baselineName": baseline["name"],
                    "baseline": baseline["prompt"],
                    "evolved": str(evolved.get("persona", "") or ""),
                    "evolvedAt": evolved.get("updated_at"),
                    "evolvedReason": str(evolved.get("reason", "") or ""),
                },
                "memory": {
                    "count": len(memories),
                    "items": [self._memory_item(item) for item in memories[:20]],
                },
                "live2d": {
                    "expressions": [
                        {
                            "keywords": item.get("keywords", []),
                            "expression": item.get("expression", ""),
                            "motion": item.get("motion", ""),
                        }
                        for item in self.settings.data.get("expression_map", [])
                    ],
                    "supports": ["expression", "motion", "segments"],
                },
                "features": features,
            }
        )

    async def _memory(self, request: web.Request) -> web.Response:
        user_id = self._user_id(request.query.get("user_id"))
        memories = self._personal_memories(user_id)
        memories.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return _json(
            {
                "ok": True,
                "user": user_id,
                "count": len(memories),
                "items": [self._memory_item(item) for item in memories[:50]],
            }
        )

    async def _chat(self, request: web.Request) -> web.Response:
        payload = await _read_json(request)

        protocol = payload.get("protocolVersion")
        if protocol is not None and protocol != PROTOCOL_VERSION:
            return _error(
                f"不支持的 protocolVersion: {protocol}", 400, "PROTOCOL_ERROR"
            )

        text = _clip(payload.get("text"), MAX_TEXT)
        if not text:
            return _error("缺少 text", 400, "INVALID_INPUT")

        request_id = str(payload.get("requestId") or "").strip()
        if request_id and not _REQUEST_ID_RE.match(request_id):
            return _error(
                "requestId 必须为 8-80 位字母、数字、下划线或连字符",
                400,
                "INVALID_INPUT",
            )
        session_id = str(payload.get("sessionId") or "").strip()
        if session_id and not _SESSION_ID_RE.match(session_id):
            return _error("sessionId 格式不合法", 400, "INVALID_INPUT")

        user_id = self._user_id(payload.get("user_id"))
        nickname = self._nickname(payload.get("nickname"))
        capabilities = (
            payload.get("capabilities")
            if isinstance(payload.get("capabilities"), dict)
            else {}
        )
        environment = payload.get("environment")
        environment_summary = self._environment_summary(environment)
        image_base64, warnings = self._parse_image(environment)

        # 同一身份 + 会话串行；requestId 五分钟内只处理一次，重复请求直接拒绝。
        scope = self._scope_key(session_id)
        if request_id and self._seen_request(scope, request_id):
            return _error(
                "重复的 requestId，已忽略本次请求", 409, "DUPLICATE_REQUEST"
            )

        async with self._session_lock(scope):
            if self.chat_mode == "astrbot":
                return await self._chat_via_pipeline(
                    payload,
                    text,
                    user_id,
                    nickname,
                    environment_summary,
                    image_base64,
                    warnings,
                    capabilities,
                    request_id,
                    scope,
                )
            return await self._chat_direct(
                payload,
                text,
                user_id,
                nickname,
                environment_summary,
                image_base64,
                warnings,
                capabilities,
                request_id,
                scope,
            )

    async def _chat_via_pipeline(
        self,
        payload: Dict[str, Any],
        text: str,
        user_id: str,
        nickname: str,
        environment_summary: str,
        image_base64: str,
        warnings: List[str],
        capabilities: Dict[str, Any],
        request_id: str,
        scope: str,
    ) -> web.Response:
        """把桌面消息注入 AstrBot 事件队列，由主 Agent 管线完成后收回文本。"""
        platform_id, platform_name, self_id = self._pipeline_target()

        try:
            reply, image_sent = await chat_via_astrbot(
                self.plugin.context,
                text=text,
                user_id=user_id,
                nickname=nickname,
                platform_id=platform_id,
                platform_name=platform_name,
                self_id=self_id,
                environment=environment_summary,
                image_base64=image_base64,
                first_timeout=PIPELINE_FIRST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return _error("模型响应超时", 504, "TIMEOUT")
        except RuntimeError as exc:
            return _error(str(exc), 504, "TIMEOUT")
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 桌面端管线聊天失败: {exc}", exc_info=True)
            return _error(f"聊天失败: {exc}", 502, "UPSTREAM_UNAVAILABLE")

        vision_used = image_sent and self._vision_provider_available()
        if image_sent and not vision_used:
            warnings.append("没有声明图片能力的 Provider，截图仅作为文字描述参考")

        self.last_chat_at = _iso_now()
        # 管线里的插件钩子已经记录演化对话、调度情绪分析并累计计数，这里不再重复记录。
        return self._success_response(
            request_id=request_id,
            scope=scope,
            reply=reply,
            user_id=user_id,
            nickname=nickname,
            vision_used=vision_used,
            warnings=warnings,
            capabilities=capabilities,
        )

    async def _chat_direct(
        self,
        payload: Dict[str, Any],
        text: str,
        user_id: str,
        nickname: str,
        environment_summary: str,
        image_base64: str,
        warnings: List[str],
        capabilities: Dict[str, Any],
        request_id: str,
        scope: str,
    ) -> web.Response:
        """插件直连 Provider 的旧链路，仅在使用本页配置的链路时启用。"""
        umo = self._umo(user_id)
        vision_used = False

        try:
            baseline = await self._baseline_persona(user_id, umo)
            affinity = self.plugin.data_manager.get_user_affinity(user_id, nickname)
            system_parts: List[str] = []
            if baseline["prompt"]:
                system_parts.append(baseline["prompt"])

            emotion_cfg = self.plugin.config.get("emotion_config", {}) or {}
            if emotion_cfg.get("available", False) and self.plugin.current_emotion:
                system_parts.append(
                    format_emotion_state_injection(
                        nickname, str(self.plugin.current_emotion), affinity
                    )
                )

            persona_cfg = self.plugin.config.get("persona_evolution_config", {}) or {}
            overlay = str(self.plugin._persona_overlay or "")
            if persona_cfg.get("available", False) and overlay:
                injection = format_persona_overlay_injection(overlay)
                if injection:
                    system_parts.append(injection)

            matched = await self._match_memories(user_id, text)
            if matched:
                injection = format_personal_memory_injection(
                    nickname,
                    [
                        {
                            "category": str(item.get("category", "")),
                            "text": _clip(item.get("text"), 80),
                        }
                        for item in matched
                    ],
                )
                if injection:
                    system_parts.append(injection)

            if environment_summary:
                system_parts.append(f"<桌面环境>\n{environment_summary}\n</桌面环境>")

            contexts = self._history_contexts(payload.get("history"))
            provider = resolve_provider(self.plugin.context, self.llm_provider_id)
            if provider is None:
                return _error("AstrBot 没有可用的文本 Provider", 503, "NOT_CONFIGURED")

            image_urls: List[str] = []
            if image_base64:
                if not self._provider_supports_image(provider):
                    warnings.append("当前 Provider 未声明图片能力，已忽略截图")
                else:
                    try:
                        path = await Image.fromBase64(
                            image_base64
                        ).convert_to_file_path()
                    except Exception as exc:
                        path = ""
                        warnings.append(f"图片转存失败：{exc}")
                    if path:
                        image_urls = [path]
                        vision_used = True

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=text,
                    contexts=contexts,
                    system_prompt="\n\n".join(system_parts),
                    image_urls=image_urls or None,
                ),
                timeout=CHAT_TIMEOUT,
            )
            reply = _clip(getattr(response, "completion_text", ""), 4000)
            if not reply:
                return _error("模型没有返回内容", 502, "UPSTREAM_UNAVAILABLE")
        except asyncio.TimeoutError:
            return _error("模型响应超时", 504, "TIMEOUT")
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 桌面端聊天失败: {exc}", exc_info=True)
            return _error(f"聊天失败: {exc}", 502, "UPSTREAM_UNAVAILABLE")

        self.last_chat_at = _iso_now()
        try:
            self._record_dialogue(umo, user_id, nickname, text, reply)
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 记录桌面对话/情绪失败: {exc}")

        return self._success_response(
            request_id=request_id,
            scope=scope,
            reply=reply,
            user_id=user_id,
            nickname=nickname,
            vision_used=vision_used,
            warnings=warnings,
            capabilities=capabilities,
        )

    async def _action_result(self, request: web.Request) -> web.Response:
        """接收桌面端的动作执行回执；只更新状态，不重新推理"""
        payload = await _read_json(request)
        if payload.get("protocolVersion") != PROTOCOL_VERSION:
            return _error(
                "动作回执仅支持 protocolVersion=2", 400, "INVALID_INPUT"
            )

        request_id = str(payload.get("requestId") or "").strip()
        if not _REQUEST_ID_RE.match(request_id):
            return _error("requestId 格式不合法", 400, "INVALID_INPUT")
        session_id = str(payload.get("sessionId") or "").strip()
        if not _SESSION_ID_RE.match(session_id):
            return _error("sessionId 格式不合法", 400, "INVALID_INPUT")
        executed_at = payload.get("executedAt")
        if (
            not isinstance(executed_at, (int, float))
            or isinstance(executed_at, bool)
        ):
            return _error("executedAt 必须为数字（Unix 毫秒）", 400, "INVALID_INPUT")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) > 8:
            return _error("results 必须为最多 8 项的数组", 400, "INVALID_INPUT")

        record = self._action_record(self._scope_key(session_id), request_id)
        if record is None:
            return _error(
                "找不到对应的聊天回复（可能已过期或不属于该会话）",
                404,
                "NOT_FOUND",
            )
        if record.get("reported"):
            return _error("该请求的动作回执已记录，忽略重复上报", 409, "DUPLICATE_RECEIPT")

        actions = list(record.get("actions") or [])
        allowed_types = {"motion", "intent", "expression", "pose", "interrupt", "reset"}
        normalized: List[Dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                return _error("回执项必须为对象", 400, "INVALID_RECEIPT")
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                return _error("回执 index 必须为非负整数", 400, "INVALID_RECEIPT")
            action_type = str(item.get("type") or "")
            if action_type not in allowed_types:
                return _error(f"未知的动作类型 {action_type}", 400, "INVALID_RECEIPT")
            status = str(item.get("status") or "")
            if status not in ("accepted", "rejected"):
                return _error(
                    "回执 status 必须为 accepted/rejected", 400, "INVALID_RECEIPT"
                )
            if index >= len(actions) or str(actions[index].get("type")) != action_type:
                return _error(
                    "回执 index/type 与已下发的动作不匹配", 400, "INVALID_RECEIPT"
                )
            normalized.append(
                {
                    "index": index,
                    "type": action_type,
                    "status": status,
                    "reason": _clip(item.get("reason"), 200),
                }
            )

        record["reported"] = True
        record["executedAt"] = executed_at
        record["results"] = normalized
        logger.info(
            f"[Fiscok's][suzu] 收到动作回执 "
            f"(requestId={request_id}, 条数={len(normalized)})"
        )
        return _json({"ok": True})

    async def _memory_note(self, request: web.Request) -> web.Response:
        payload = await _read_json(request)
        text = _clip(payload.get("text"), 500)
        if not text:
            return _error("缺少 text")
        user_id = self._user_id(payload.get("user_id"))
        category = _clip(payload.get("category"), 40) or "profile"
        importance = max(1, min(5, int(payload.get("importance") or 3)))

        result = await self.write_note(user_id, text, category, importance)
        status = int(result.pop("status", 200))
        return _json(result, status=status)

    async def write_note(
        self, user_id: str, text: str, category: str, importance: int
    ) -> Dict[str, Any]:
        """写入个人长期记忆，桌面端与插件 WebUI 共用。"""
        cfg = self.plugin.config.get("personal_memory_config", {}) or {}
        provider_id = str(cfg.get("embedding_provider_id", "") or "")
        if resolve_embedding_provider(self.plugin.context, provider_id) is None:
            return {
                "ok": False,
                "status": 409,
                "error": "AstrBot 未配置可用的 Embedding Provider，暂时无法写入个人记忆",
            }

        try:
            vector = await get_text_embedding(self.plugin.context, provider_id, text)
            if not vector:
                return {"ok": False, "status": 502, "error": "生成词向量失败，记忆未写入"}
            memory_id = self.plugin.data_manager.add_personal_memory(
                user_id, category, text, vector, importance
            )
            if not memory_id:
                return {"ok": False, "status": 500, "error": "写入个人记忆失败"}
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 写入个人记忆失败: {exc}", exc_info=True)
            return {"ok": False, "status": 502, "error": f"写入失败: {exc}"}

        return {"ok": True, "id": memory_id, "user": user_id}

    async def _persona_evolve(self, request: web.Request) -> web.Response:
        payload = await _read_json(request)
        user_id = self._user_id(payload.get("user_id"))
        result = await self.evolve_now(user_id, _clip(payload.get("dialogue"), 2000))
        status = int(result.pop("status", 200))
        return _json(result, status=status)

    async def evolve_now(
        self, user_id: str, dialogue: str = ""
    ) -> Dict[str, Any]:
        """手动触发一次人设演化，桌面端与插件 WebUI 共用。"""
        umo = self._evolution_umo(user_id, self._umo(user_id))
        cfg = self.plugin.config.get("persona_evolution_config", {}) or {}
        if not cfg.get("available", False):
            return {
                "ok": False,
                "status": 409,
                "error": "人设演化未开启（persona_evolution_config.available）",
            }

        if not dialogue:
            dialogue = "\n".join(self.plugin._persona_recent.get(umo, []))
        if not dialogue:
            return {"ok": False, "status": 409, "error": "还没有可用于演化的对话"}

        if self._evolve_lock.locked():
            return {"ok": False, "status": 409, "error": "正在演化中，请稍候"}
        async with self._evolve_lock:
            current = str(self.plugin._persona_overlay or "")
            try:
                result = await evolve_persona(
                    self.plugin.context,
                    provider_id=str(cfg.get("llm_provider_id", "") or ""),
                    current_persona=current,
                    dialogue=dialogue,
                    max_chars=int(cfg.get("max_chars", 200) or 200),
                    timeout=30.0,
                )
            except Exception as exc:
                logger.error(f"[Fiscok's][suzu] 手动人设演化失败: {exc}", exc_info=True)
                return {"ok": False, "status": 502, "error": f"演化失败: {exc}"}
            if not result:
                return {"ok": False, "status": 502, "error": "演化模型没有返回有效结果"}

            persona = str(result.get("persona", "") or "").strip()
            reason = str(result.get("reason", "") or "").strip()
            changed = bool(result.get("changed", False)) and persona != current
            if changed and persona:
                if not self.plugin.data_manager.save_evolved_persona(persona, reason):
                    return {"ok": False, "status": 500, "error": "保存演化人设失败"}
                self.plugin._persona_overlay = persona

        saved = self.plugin.data_manager.get_evolved_persona() or {}
        return {
            "ok": True,
            "changed": changed,
            "persona": str(saved.get("persona", "") or current),
            "reason": str(saved.get("reason", "") or ""),
            "updatedAt": saved.get("updated_at"),
        }

    # ---------------- 对话辅助 ----------------

    def _history_contexts(self, history: Any) -> List[Dict[str, str]]:
        contexts: List[Dict[str, str]] = []
        if not isinstance(history, list):
            return contexts
        for item in history[-MAX_HISTORY:]:
            if not isinstance(item, dict):
                continue
            text = _clip(item.get("text"), 800)
            if not text:
                continue
            role = "assistant" if item.get("role") in ("pet", "assistant", "bot") else "user"
            contexts.append({"role": role, "content": text})
        return contexts

    def _record_dialogue(
        self, umo: str, user_id: str, nickname: str, user_text: str, reply: str
    ) -> None:
        """桌面端复用插件演化方案：绑定 QQ 时与该账号共用同一份演化对话与结果。"""
        persona_cfg = self.plugin.config.get("persona_evolution_config", {}) or {}
        if persona_cfg.get("available", False):
            evo_umo = self._evolution_umo(user_id, umo)
            self.plugin._append_persona_line(evo_umo, nickname, user_text, persona_cfg)
            self.plugin._append_persona_line(evo_umo, "我", reply, persona_cfg)
            every = int(persona_cfg.get("evolve_every_messages", 10) or 0)
            if every > 0:
                self.plugin._persona_reply_count += 1
                if (
                    self.plugin._persona_reply_count >= every
                    and not self.plugin._persona_evolve_pending
                ):
                    self.plugin._persona_reply_count = 0
                    self.plugin._persona_evolve_pending = True
                    dialogue = "\n".join(self.plugin._persona_recent.get(evo_umo, []))
                    if dialogue:
                        self.plugin._spawn_background(
                            self.plugin._run_persona_evolution(dialogue, persona_cfg)
                        )

        emotion_cfg = self.plugin.config.get("emotion_config", {}) or {}
        if emotion_cfg.get("available", False):
            self.plugin._schedule_emotion_analysis(
                user_id, nickname, user_text, emotion_cfg
            )
