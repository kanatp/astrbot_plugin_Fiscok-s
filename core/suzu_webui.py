"""Fiscok 插件 WebUI：NapCat 风格的本地控制台。

与桌面桥接共用一个 aiohttp 应用：
- `/` 与 `/ui/*` 走账号密码会话（本文件）
- `/suzu/*` 走桌面端 Bearer Token（suzu_bridge.py）

密码在首次启动时随机生成并保存在插件数据目录，只输出到 AstrBot 本机日志，
与 NapCat 在控制台打印 token 的做法一致。凭据无法落盘时仍使用内存中的密码运行，
不会阻断桥接启动（只是重启后密码会重新生成）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

from astrbot.api import logger

from .api.topic_memory_apis import get_text_embedding

SESSION_TTL = 12 * 3600
COOKIE_NAME = "suzu_ui"
LOGIN_WINDOW = 600.0
LOGIN_MAX_FAILURES = 5

# Fiscok_webui 允许在线编辑的配置分区（其余分区请到 AstrBot 插件配置页修改）
EDITABLE_CONFIG_SECTIONS = (
    "meme_config",
    "topic_memory_config",
    "personal_memory_config",
    "emotion_config",
    "persona_evolution_config",
)
PASSWORD_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyz"


def _iso_now() -> str:
    return datetime.now().isoformat()


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False)
    )


async def _read_json(request: web.Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


class SuzuWebUI:
    """插件侧控制台；由 SuzuBridge 持有并注册路由。"""

    def __init__(self, bridge: Any):
        self.bridge = bridge
        self.plugin = bridge.plugin
        self.username = "suzu"
        self.password = ""
        self._secret = ""
        self._failures: Dict[str, List[float]] = {}
        self._auth_path = self._resolve_auth_path()
        self._html = self._load_html()
        self._load_auth()

    def _resolve_auth_path(self) -> Path:
        """凭据放在 AstrBot 的 data/plugin_data/<插件名>/ 下，取不到时退回当前目录。"""
        name = str(getattr(self.plugin, "name", "") or "astrbot_plugin_Fiscok")
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:
            base = Path("data") / "plugin_data"
        return base / name / "suzu_webui.json"

    # ---------------- 账号 ----------------

    def _load_auth(self) -> None:
        data: Dict[str, Any] = {}
        try:
            raw = self._auth_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[Fiscok's][suzu] 读取 WebUI 凭据失败，将重新生成: {exc}")
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 读取 WebUI 凭据异常，将重新生成: {exc}")

        self.username = str(data.get("username") or "suzu")[:40]
        self.password = str(data.get("password") or "")
        self._secret = str(data.get("secret") or "")

        fresh = False
        if not self.password or not self._secret:
            self.password = "".join(
                secrets.choice(PASSWORD_ALPHABET) for _ in range(10)
            )
            self._secret = secrets.token_hex(32)
            fresh = True
            if self._save_auth():
                logger.info(
                    "[Fiscok's][suzu] WebUI 凭据已写入插件数据目录"
                )
            else:
                logger.warning(
                    "[Fiscok's][suzu] WebUI 凭据未能落盘，本次运行有效，重启后密码会变化"
                )
        logger.info(
            f"[Fiscok's][suzu] Kira Live 插件 WebUI {'已生成' if fresh else '已加载'}登录密码："
            f"账号 {self.username}，密码 {self.password}（仅本机日志可见）"
        )

    def _save_auth(self) -> bool:
        try:
            self._auth_path.parent.mkdir(parents=True, exist_ok=True)
            self._auth_path.write_text(
                json.dumps(
                    {
                        "username": self.username,
                        "password": self.password,
                        "secret": self._secret,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 保存 WebUI 凭据失败: {exc}")
            return False

    def _load_html(self) -> str:
        try:
            return Path(__file__).with_name("suzu_webui.html").read_text(encoding="utf-8")
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 读取 WebUI 页面失败: {exc}")
            return "<h1>Kira Live 插件 WebUI</h1><p>页面文件缺失，请检查 suzu_webui.html</p>"

    # ---------------- 会话 ----------------

    def _issue_session(self) -> str:
        expires = int(time.time()) + SESSION_TTL
        signature = hmac.new(
            self._secret.encode("utf-8"), str(expires).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{expires}.{signature}"

    def _authorized(self, request: web.Request) -> bool:
        """只依赖内存里的密钥，凭据文件丢失也不会让已登录会话失效。"""
        raw = request.cookies.get(COOKIE_NAME, "")
        if "." not in raw:
            return False
        expires_text, signature = raw.split(".", 1)
        try:
            expires = int(expires_text)
        except ValueError:
            return False
        expected = hmac.new(
            self._secret.encode("utf-8"), str(expires).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return expires > int(time.time()) and hmac.compare_digest(signature, expected)

    def _guard(self, request: web.Request) -> Optional[web.Response]:
        if self._authorized(request):
            return None
        return _json({"ok": False, "error": "登录状态已失效，请重新登录"}, 401)

    def _client_ip(self, request: web.Request) -> str:
        try:
            return self.bridge._client_ip(request) or "unknown"
        except Exception:
            return "unknown"

    def _blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [item for item in self._failures.get(ip, []) if now - item < LOGIN_WINDOW]
        self._failures[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILURES

    def _record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())

    # ---------------- 路由 ----------------

    def register(self, app: web.Application) -> None:
        app.router.add_get("/", self._page)
        app.router.add_get("/ui/api/session", self._session)
        app.router.add_post("/ui/api/login", self._login)
        app.router.add_post("/ui/api/logout", self._logout)
        app.router.add_get("/ui/api/state", self._state)
        app.router.add_post("/ui/api/note", self._note)
        app.router.add_post("/ui/api/evolve", self._evolve)
        app.router.add_get("/ui/api/memes", self._memes)
        app.router.add_get("/ui/api/memes/{meme_id}/image", self._meme_image)
        app.router.add_post("/ui/api/memes/update", self._meme_update)
        app.router.add_post("/ui/api/memes/delete", self._meme_delete)
        app.router.add_post("/ui/api/memes/clear", self._meme_clear)
        app.router.add_get("/ui/api/topics", self._topics)
        app.router.add_post("/ui/api/topics/update", self._topic_update)
        app.router.add_post("/ui/api/topics/delete", self._topic_delete)
        app.router.add_post("/ui/api/topics/clear", self._topic_clear)
        app.router.add_get("/ui/api/personal", self._personal)
        app.router.add_post("/ui/api/personal/update", self._personal_update)
        app.router.add_post("/ui/api/personal/delete", self._personal_delete)
        app.router.add_post("/ui/api/personal/clear", self._personal_clear)
        app.router.add_get("/ui/api/config", self._config_get)
        app.router.add_post("/ui/api/config", self._config_set)
        app.router.add_get("/ui/api/emotion", self._emotion_get)
        app.router.add_post("/ui/api/emotion", self._emotion_set)
        app.router.add_post("/ui/api/affinity", self._affinity_set)
        app.router.add_get("/ui/api/live2d", self._live2d_get)
        app.router.add_post("/ui/api/live2d", self._live2d_set)
        app.router.add_post("/ui/api/live2d/restart", self._live2d_restart)

    async def _page(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _session(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return _json({"ok": False, "error": "未登录"}, 401)
        return _json({"ok": True, "username": self.username})

    async def _login(self, request: web.Request) -> web.Response:
        ip = self._client_ip(request)
        if self._blocked(ip):
            return _json({"ok": False, "error": "尝试次数过多，请 10 分钟后再试"}, 429)
        payload = await _read_json(request)
        supplied = str(payload.get("password") or "")
        if not hmac.compare_digest(
            supplied.encode("utf-8"), self.password.encode("utf-8")
        ):
            self._record_failure(ip)
            return _json({"ok": False, "error": "密码不正确"}, 401)
        self._failures.pop(ip, None)
        response = _json({"ok": True, "username": self.username})
        response.set_cookie(
            COOKIE_NAME,
            self._issue_session(),
            max_age=SESSION_TTL,
            httponly=True,
            samesite="Strict",
            secure=request.secure,
            path="/",
        )
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        response = _json({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def _state(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        return _json(await self._snapshot())

    async def _note(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        text = str(payload.get("text") or "").strip()[:500]
        if not text:
            return _json({"ok": False, "error": "记忆内容不能为空"}, 400)
        category = str(payload.get("category") or "profile")[:40]
        importance = max(1, min(5, int(payload.get("importance") or 3)))
        result = await self.bridge.write_note(
            self.bridge.memory_user_id, text, category, importance
        )
        if not result.get("ok"):
            return _json(result, status=int(result.get("status") or 502))
        snapshot = await self._snapshot()
        snapshot["result"] = result
        return _json(snapshot)

    async def _evolve(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        dialogue = str(payload.get("dialogue") or "")[:2000]
        result = await self.bridge.evolve_now(self.bridge.memory_user_id, dialogue)
        if not result.get("ok"):
            return _json(result, status=int(result.get("status") or 502))
        snapshot = await self._snapshot()
        snapshot["result"] = result
        return _json(snapshot)

    # ---------------- 数据 ----------------

    async def _snapshot(self) -> Dict[str, Any]:
        bridge = self.bridge
        plugin = self.plugin
        user_id = bridge.memory_user_id
        umo = bridge._umo(user_id)

        try:
            baseline = await bridge._baseline_persona(umo)
        except Exception:
            baseline = {"name": "", "prompt": ""}
        try:
            evolved = plugin.data_manager.get_evolved_persona() or {}
        except Exception:
            evolved = {}
        try:
            affinity = plugin.data_manager.get_user_affinity(
                user_id, bridge.default_nickname
            )
        except Exception:
            affinity = 0
        try:
            memories = bridge._personal_memories(user_id)
            memories.sort(
                key=lambda item: str(
                    item.get("updated_at") or item.get("created_at") or ""
                ),
                reverse=True,
            )
        except Exception:
            memories = []

        host = bridge.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        address = f"http://{host}:{bridge.port}/suzu"
        features = {
            "meme": bool(
                (plugin.config.get("meme_config", {}) or {}).get(
                    "meme_available", False
                )
            ),
            "topicMemory": bool(
                (plugin.config.get("topic_memory_config", {}) or {}).get(
                    "available", False
                )
            ),
            "personaEvolution": bool(
                (plugin.config.get("persona_evolution_config", {}) or {}).get(
                    "available", False
                )
            ),
            "emotion": bool(
                (plugin.config.get("emotion_config", {}) or {}).get("available", False)
            ),
            "personalMemory": bool(
                (plugin.config.get("personal_memory_config", {}) or {}).get(
                    "available", False
                )
            ),
        }
        return {
            "ok": True,
            "time": _iso_now(),
            "bridge": {
                "running": bridge._started,
                "host": bridge.host,
                "port": bridge.port,
                "address": address,
                "token": bridge.token,
                "hasToken": bool(bridge.token),
                "localOnly": bridge.local_only,
                "whitelist": [str(item) for item in bridge.allowed_networks],
                "trusted": [str(item) for item in bridge.trusted_networks],
                "requests": bridge.request_count,
                "rejected": bridge.reject_count,
            },
            "desktop": {
                "last": bridge.last_client,
                "bootstrapAt": bridge.last_bootstrap_at,
                "chatAt": bridge.last_chat_at,
            },
            "user": {
                "id": user_id,
                "boundQQ": bridge.memory_qq,
                "nickname": bridge.default_nickname,
                "affinity": affinity,
            },
            "persona": {
                "baselineName": baseline.get("name") or "",
                "baseline": baseline.get("prompt") or "",
                "emotion": str(plugin.current_emotion or ""),
                "evolved": str(evolved.get("persona", "") or ""),
                "evolvedAt": evolved.get("updated_at"),
                "evolvedReason": str(evolved.get("reason", "") or ""),
            },
            "memory": {
                "count": len(memories),
                "items": [bridge._memory_item(item) for item in memories[:30]],
            },
            "features": features,
        }

    # ---------------- 表情包 ----------------

    async def _memes(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        manager = self.plugin.data_manager
        keyword = str(request.query.get("q") or "").strip().lower()
        tag_filter = str(request.query.get("tag") or "").strip()
        emotion_filter = str(request.query.get("emotion") or "").strip()
        items = []
        tag_counts: Dict[str, int] = {}
        emotion_counts: Dict[str, int] = {}
        try:
            memes = manager.get_memes()
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 读取表情包库失败: {exc}")
            return _json({"ok": False, "error": "读取表情包库失败"}, 500)
        total = len(memes)
        for meme in memes:
            tags = [str(item) for item in (meme.get("tags") or []) if str(item).strip()]
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            emotion = str(meme.get("emotion") or "").strip()
            if emotion:
                emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
            description = str(meme.get("description") or "")
            if keyword and keyword not in description.lower() and not any(
                keyword in tag.lower() for tag in tags
            ):
                continue
            if tag_filter and tag_filter not in tags:
                continue
            if emotion_filter and str(meme.get("emotion") or "") != emotion_filter:
                continue
            items.append({
                "id": str(meme.get("id") or ""),
                "description": description,
                "tags": tags,
                "emotion": str(meme.get("emotion") or ""),
                "timestamp": str(meme.get("timestamp") or ""),
                "source": str(meme.get("source") or ""),
                "url": f"/ui/api/memes/{meme.get('id')}/image",
            })
        return _json({
            "ok": True,
            "total": total,
            "items": items,
            "tags": [
                {"name": name, "count": count}
                for name, count in sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0]))
            ],
            "emotions": [
                {"name": name, "count": count}
                for name, count in sorted(emotion_counts.items(), key=lambda pair: (-pair[1], pair[0]))
            ],
        })

    async def _meme_image(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        meme_id = str(request.match_info.get("meme_id") or "")
        try:
            path = self.plugin.data_manager.get_meme_path(meme_id)
        except Exception:
            path = None
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=300"})

    async def _meme_update(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        meme_id = str(payload.get("id") or "")
        if not meme_id:
            return _json({"ok": False, "error": "缺少表情包 ID"}, 400)
        changes: Dict[str, Any] = {}
        if "description" in payload:
            changes["description"] = str(payload.get("description") or "").strip()[:300]
        if "emotion" in payload:
            changes["emotion"] = str(payload.get("emotion") or "").strip()[:20]
        if "tags" in payload:
            raw = payload.get("tags")
            if isinstance(raw, str):
                parts = raw.replace("，", ",").replace("、", ",").split(",")
            elif isinstance(raw, list):
                parts = raw
            else:
                parts = []
            tags: List[str] = []
            for part in parts:
                text = str(part).strip()[:24]
                if text and text not in tags:
                    tags.append(text)
            changes["tags"] = tags[:12]
        if not changes:
            return _json({"ok": False, "error": "没有可更新的字段"}, 400)
        ok = self.plugin.data_manager.update_meme(meme_id, **changes)
        if not ok:
            return _json({"ok": False, "error": "表情包不存在"}, 404)
        return _json({"ok": True})

    # ---------------- 话题记忆 ----------------

    async def _topics(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        manager = self.plugin.data_manager
        try:
            overview = manager.get_topic_overview()
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 读取话题记忆失败: {exc}")
            return _json({"ok": False, "error": "读取话题记忆失败"}, 500)
        scope = str(request.query.get("scope") or "")
        if not scope and overview:
            scope = str(overview[0].get("scope") or "")
        items = []
        if scope:
            try:
                entries = manager.get_topics(scope)
            except Exception:
                entries = []
            entries.sort(
                key=lambda item: str(item.get("updated_at") or item.get("timestamp") or ""),
                reverse=True,
            )
            items = [
                {
                    "id": str(item.get("id") or ""),
                    "text": str(item.get("text") or ""),
                    "importance": int(item.get("importance") or 3),
                    "ownerId": str(item.get("owner_id") or ""),
                    "ownerName": str(item.get("owner_name") or ""),
                    "timestamp": str(item.get("timestamp") or ""),
                    "updatedAt": str(item.get("updated_at") or ""),
                }
                for item in entries
            ]
        return _json({
            "ok": True,
            "scope": scope,
            "scopes": overview,
            "items": items,
            "embedding": bool(
                (self.plugin.config.get("topic_memory_config", {}) or {}).get(
                    "embedding_provider_id", ""
                )
            ),
        })

    async def _topic_update(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        topic_id = str(payload.get("id") or "")
        if not topic_id:
            return _json({"ok": False, "error": "缺少话题记忆 ID"}, 400)
        manager = self.plugin.data_manager
        importance: Optional[int] = None
        if "importance" in payload:
            importance = max(1, min(5, int(payload.get("importance") or 3)))
        text: Optional[str] = None
        vector: Optional[List[float]] = None
        if "text" in payload:
            text = str(payload.get("text") or "").strip()[:4000]
            if not text:
                return _json({"ok": False, "error": "记忆内容不能为空"}, 400)
            provider_id = str(
                (self.plugin.config.get("topic_memory_config", {}) or {}).get(
                    "embedding_provider_id", ""
                )
            )
            try:
                vector = await get_text_embedding(self.plugin.context, provider_id, text)
            except Exception as exc:
                logger.error(f"[Fiscok's][suzu] 重算话题向量失败: {exc}")
                vector = None
            if not vector:
                return _json(
                    {"ok": False, "error": "未能生成新向量，请先在 AstrBot 配置 Embedding Provider"},
                    409,
                )
        if text is None and importance is None:
            return _json({"ok": False, "error": "没有可更新的字段"}, 400)
        ok = manager.update_topic(
            topic_id, text=text or "", vector=vector, importance=importance
        )
        if not ok:
            return _json({"ok": False, "error": "话题记忆不存在"}, 404)
        return _json({"ok": True})

    # ---------------- 私聊记忆 ----------------

    async def _personal(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        manager = self.plugin.data_manager
        try:
            overview = manager.get_personal_memory_overview()
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 读取个人记忆失败: {exc}")
            return _json({"ok": False, "error": "读取个人记忆失败"}, 500)
        user_id = str(request.query.get("user_id") or "")
        if not user_id:
            preferred = self.bridge.memory_user_id
            if any(str(item.get("user_id")) == preferred for item in overview):
                user_id = preferred
            elif overview:
                user_id = str(overview[0].get("user_id") or "")
        items = []
        if user_id:
            try:
                entries = manager.get_personal_memories(user_id)
            except Exception:
                entries = []
            entries.sort(
                key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                reverse=True,
            )
            items = [
                {
                    "id": str(item.get("id") or ""),
                    "text": str(item.get("text") or ""),
                    "category": str(item.get("category") or "profile"),
                    "importance": int(item.get("importance") or 3),
                    "createdAt": str(item.get("created_at") or ""),
                    "updatedAt": str(item.get("updated_at") or ""),
                }
                for item in entries
            ]
        return _json({
            "ok": True,
            "userId": user_id,
            "users": overview,
            "items": items,
            "embedding": bool(
                (self.plugin.config.get("personal_memory_config", {}) or {}).get(
                    "embedding_provider_id", ""
                )
            ),
        })

    async def _personal_update(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        user_id = str(payload.get("userId") or self.bridge.memory_user_id)
        memory_id = str(payload.get("id") or "")
        if not memory_id:
            return _json({"ok": False, "error": "缺少记忆 ID"}, 400)
        manager = self.plugin.data_manager
        importance: Optional[int] = None
        if "importance" in payload:
            importance = max(1, min(5, int(payload.get("importance") or 3)))
        text: Optional[str] = None
        vector: Optional[List[float]] = None
        if "text" in payload:
            text = str(payload.get("text") or "").strip()[:4000]
            if not text:
                return _json({"ok": False, "error": "记忆内容不能为空"}, 400)
            provider_id = str(
                (self.plugin.config.get("personal_memory_config", {}) or {}).get(
                    "embedding_provider_id", ""
                )
            )
            try:
                vector = await get_text_embedding(self.plugin.context, provider_id, text)
            except Exception as exc:
                logger.error(f"[Fiscok's][suzu] 重算个人记忆向量失败: {exc}")
                vector = None
            if not vector:
                return _json(
                    {"ok": False, "error": "未能生成新向量，请先在 AstrBot 配置 Embedding Provider"},
                    409,
                )
        if text is None and importance is None:
            return _json({"ok": False, "error": "没有可更新的字段"}, 400)
        ok = manager.update_personal_memory(
            user_id, memory_id, text=text, vector=vector, importance=importance
        )
        if not ok:
            return _json({"ok": False, "error": "个人记忆不存在"}, 404)
        return _json({"ok": True})

    # ---------------- 删除 / 清空 ----------------

    async def _meme_delete(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        meme_id = str(payload.get("id") or "")
        if not meme_id:
            return _json({"ok": False, "error": "缺少表情包 ID"}, 400)
        if not self.plugin.data_manager.delete_meme(meme_id):
            return _json({"ok": False, "error": "表情包不存在"}, 404)
        return _json({"ok": True})

    async def _meme_clear(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        removed = self.plugin.data_manager.clear_memes()
        return _json({"ok": True, "removed": removed})

    async def _topic_delete(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        topic_id = str(payload.get("id") or "")
        if not topic_id:
            return _json({"ok": False, "error": "缺少话题记忆 ID"}, 400)
        if not self.plugin.data_manager.delete_topic(topic_id):
            return _json({"ok": False, "error": "话题记忆不存在"}, 404)
        return _json({"ok": True})

    async def _topic_clear(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        scope = str(payload.get("scope") or "").strip()
        if not scope:
            return _json({"ok": False, "error": "缺少会话 scope"}, 400)
        self.plugin.data_manager.clear_topics(scope)
        return _json({"ok": True})

    async def _personal_delete(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        user_id = str(payload.get("userId") or "")
        memory_id = str(payload.get("id") or "")
        if not user_id or not memory_id:
            return _json({"ok": False, "error": "缺少用户 ID 或记忆 ID"}, 400)
        if not self.plugin.data_manager.delete_personal_memory(user_id, memory_id):
            return _json({"ok": False, "error": "个人记忆不存在"}, 404)
        return _json({"ok": True})

    async def _personal_clear(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        user_id = str(payload.get("userId") or "")
        if not user_id:
            return _json({"ok": False, "error": "缺少用户 ID"}, 400)
        self.plugin.data_manager.clear_personal_memories(user_id)
        return _json({"ok": True})

    # ---------------- 配置读写 ----------------

    def _load_schema(self) -> Dict[str, Any]:
        path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 读取配置 schema 失败: {exc}")
            return {}

    async def _config_get(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        schema = self._load_schema()
        sections = []
        for section_key in EDITABLE_CONFIG_SECTIONS:
            section_schema = schema.get(section_key) or {}
            fields = []
            current = self.plugin.config.get(section_key) or {}
            for field_key, meta in (section_schema.get("items") or {}).items():
                if not isinstance(meta, dict):
                    continue
                field_type = str(meta.get("type") or "")
                if field_type not in ("bool", "int", "float", "string"):
                    continue
                value = current.get(field_key, meta.get("default"))
                fields.append({
                    "key": field_key,
                    "label": str(meta.get("description") or field_key),
                    "type": field_type,
                    "value": value,
                    "hint": str(meta.get("hint") or ""),
                })
            if not fields:
                continue
            sections.append({
                "key": section_key,
                "title": str(section_schema.get("description") or section_key),
                "fields": fields,
            })
        return _json({"ok": True, "sections": sections})

    async def _config_set(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            return _json({"ok": False, "error": "没有可保存的配置项"}, 400)
        schema = self._load_schema()
        updated = 0
        for item in values:
            if not isinstance(item, dict):
                continue
            section_key = str(item.get("section") or "")
            field_key = str(item.get("key") or "")
            if section_key not in EDITABLE_CONFIG_SECTIONS:
                return _json({"ok": False, "error": f"不允许修改配置分区: {section_key}"}, 400)
            meta = ((schema.get(section_key) or {}).get("items") or {}).get(field_key)
            if not isinstance(meta, dict):
                return _json({"ok": False, "error": f"未知配置项: {section_key}.{field_key}"}, 400)
            field_type = str(meta.get("type") or "")
            raw = item.get("value")
            try:
                if field_type == "bool":
                    if isinstance(raw, bool):
                        value: Any = raw
                    else:
                        value = str(raw).strip().lower() in ("1", "true", "on", "yes")
                elif field_type == "int":
                    value = int(float(raw))
                elif field_type == "float":
                    value = float(raw)
                elif field_type == "string":
                    value = str(raw or "")[:300]
                else:
                    continue
            except (TypeError, ValueError):
                return _json(
                    {"ok": False, "error": f"配置项 {field_key} 的值格式不正确"}, 400
                )
            config = self.plugin.config
            if section_key not in config:
                config[section_key] = {}
            config[section_key][field_key] = value
            updated += 1
        try:
            save_async = getattr(self.plugin.config, "save_config_async", None)
            if callable(save_async):
                await save_async()
            else:
                save = getattr(self.plugin.config, "save_config", None)
                if callable(save):
                    await asyncio.to_thread(save)
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 保存插件配置失败: {exc}")
            return _json({"ok": False, "error": "配置已更新，但写入配置文件失败"}, 500)
        return _json({"ok": True, "updated": updated})

    # ---------------- 情绪与好感度 ----------------

    def _emotion_config(self) -> Dict[str, Any]:
        config = self.plugin.config.get("emotion_config", {}) or {}
        return {
            "available": bool(config.get("available", False)),
            "init": int(config.get("affinity_init", 50)),
            "min": int(config.get("affinity_min", 0)),
            "max": int(config.get("affinity_max", 100)),
        }

    async def _emotion_get(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        try:
            users = self.plugin.data_manager.get_affinity_overview()
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 读取好感度失败: {exc}")
            users = []
        return _json({
            "ok": True,
            "emotion": str(self.plugin.current_emotion or ""),
            "config": self._emotion_config(),
            "users": users,
        })

    async def _emotion_set(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        emotion = str(payload.get("emotion") or "").strip()[:100]
        if not emotion:
            return _json({"ok": False, "error": "情绪不能为空"}, 400)
        self.plugin.current_emotion = emotion
        if bool(payload.get("persist")):
            config = self.plugin.config
            if "emotion_config" not in config:
                config["emotion_config"] = {}
            config["emotion_config"]["emotion_init"] = emotion
            try:
                save_async = getattr(config, "save_config_async", None)
                if callable(save_async):
                    await save_async()
                else:
                    save = getattr(config, "save_config", None)
                    if callable(save):
                        await asyncio.to_thread(save)
            except Exception as exc:
                logger.error(f"[Fiscok's][suzu] 保存默认情绪失败: {exc}")
                return _json({"ok": False, "error": "情绪已生效，但写入默认值失败"}, 500)
        return _json({"ok": True, "emotion": emotion})

    async def _affinity_set(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            return _json({"ok": False, "error": "缺少用户 ID"}, 400)
        try:
            affinity = int(payload.get("affinity"))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "好感度必须是整数"}, 400)
        meta = self._emotion_config()
        result = self.plugin.data_manager.set_user_affinity(
            user_id,
            affinity,
            str(payload.get("nickname") or ""),
            min_=meta["min"],
            max_=meta["max"],
        )
        return _json({"ok": True, "affinity": result.get("affinity", affinity)})

    # ---------------- Live2D 设置 ----------------

    def _providers(self) -> List[Dict[str, str]]:
        """列出可用于桌面聊天的文本 Provider（只用于下拉选择）"""
        try:
            providers = self.plugin.context.get_all_providers()
        except Exception as exc:
            logger.warning(f"[Fiscok's][suzu] 读取 Provider 列表失败: {exc}")
            return []
        result: List[Dict[str, str]] = []
        for provider in providers:
            config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(config.get("id") or "").strip()
            if not provider_id:
                continue
            model = str(
                config.get("model")
                or (config.get("model_config") or {}).get("model")
                or ""
            ).strip()
            result.append({
                "id": provider_id,
                "label": f"{provider_id}（{model}）" if model else provider_id,
            })
        return result

    @staticmethod
    def _parse_expression_map(text: str) -> List[Dict[str, Any]]:
        """把「关键词1,关键词2 = 表情 / 动作」逐行解析成映射表"""
        result: List[Dict[str, Any]] = []
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if "=>" in raw:
                left, right = raw.split("=>", 1)
            elif "=" in raw:
                left, right = raw.split("=", 1)
            else:
                continue
            keywords = [
                item.strip()
                for item in left.replace("，", ",").split(",")
                if item.strip()
            ]
            parts = [
                item.strip()
                for item in right.replace("，", "/").split("/")
                if item.strip()
            ]
            if not keywords or not parts:
                continue
            result.append({
                "keywords": keywords[:10],
                "expression": parts[0][:40],
                "motion": (parts[1] if len(parts) > 1 else "")[:40],
            })
            if len(result) >= 20:
                break
        return result

    async def _live2d_get(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        return _json({
            "ok": True,
            "settings": self.bridge.settings_view(),
            "providers": self._providers(),
        })

    async def _live2d_set(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        payload = await _read_json(request)
        changes: Dict[str, Any] = {}
        for key in ("token", "llm_provider_id", "default_user_id", "memory_qq", "default_nickname"):
            if key in payload:
                changes[key] = payload.get(key)
        if "allowed_ips" in payload:
            changes["allowed_ips"] = payload.get("allowed_ips")
        if "trusted_proxies" in payload:
            changes["trusted_proxies"] = payload.get("trusted_proxies")
        if "expression_map_text" in payload:
            changes["expression_map"] = self._parse_expression_map(
                payload.get("expression_map_text")
            )
        if not changes:
            return _json({"ok": False, "error": "没有可保存的设置"}, 400)
        try:
            data = self.bridge.save_settings(changes)
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 保存 Live2D 设置失败: {exc}")
            return _json({"ok": False, "error": f"保存失败: {exc}"}, 500)
        return _json({"ok": True, "settings": data})

    async def _live2d_restart(self, request: web.Request) -> web.Response:
        guard = self._guard(request)
        if guard:
            return guard
        # 当前请求本身就跑在这个 aiohttp 应用里，先返回响应再重建监听，避免连接被直接掐断。
        asyncio.create_task(self._restart_bridge_later())
        return _json({"ok": True, "pending": True, "settings": self.bridge.settings_view()})

    async def _restart_bridge_later(self) -> None:
        await asyncio.sleep(0.6)
        try:
            result = await self.bridge.restart()
        except Exception as exc:
            logger.error(f"[Fiscok's][suzu] 重启服务失败: {exc}", exc_info=True)
            return
        if not result.get("ok"):
            logger.error(
                f"[Fiscok's][suzu] 端口 {result.get('port')} 启动失败，"
                "请检查插件配置里的监听地址/端口是否被占用"
            )
