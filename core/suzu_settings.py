'''
Live2D 桌面端运行期设置。

监听地址 / 端口属于启动前必须知道的引导配置，仍放在 AstrBot 插件配置；
访问令牌、来源白名单、可信代理、桌面聊天 Provider、桌面身份与表情动作映射
全部改由 WebUI 的 Live2D 标签页维护，落到插件数据目录里的 suzu_live2d.json。
'''
import json
from pathlib import Path
from typing import Any, Dict, List

from astrbot.api import logger

DEFAULTS: Dict[str, Any] = {
    "token": "",
    "allowed_ips": [],
    "trusted_proxies": ["127.0.0.1", "::1"],
    "chat_mode": "astrbot",
    "vision_input": True,
    "llm_provider_id": "",
    "default_user_id": "desktop_suzu",
    "memory_qq": "",
    "persona_id": "",
    "default_nickname": "小主人",
}


def _clean_list(value: Any, limit: int = 50, item_limit: int = 64) -> List[str]:
    """把任意输入整理成去重后的字符串列表"""
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    result: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and len(text) <= item_limit and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


class Live2DSettings:
    """Live2D 设置的读取、校验与持久化"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.path = Path(getattr(plugin, "plugin_data_path", "")) / "suzu_live2d.json"
        self.data = self.load()

    def load(self) -> Dict[str, Any]:
        """读取设置文件，缺失或损坏时回退默认值"""
        raw: Dict[str, Any] = {}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = loaded
            except Exception as e:
                logger.warning(f"[Fiscok's][live2d] 读取设置失败，使用默认值: {e}")
        return self.normalize(raw)

    def save(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验并写入设置，返回生效后的完整设置"""
        self.data = self.normalize({**self.data, **(payload or {})})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Fiscok's][live2d] 保存设置失败: {e}")
        return self.data

    @staticmethod
    def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
        """把外部输入归一成合法设置"""
        raw = raw if isinstance(raw, dict) else {}

        def text(key: str, default: str, limit: int) -> str:
            value = str(raw.get(key, default) or "").strip()
            return value[:limit]

        def flag(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        return {
            "token": text("token", "", 200),
            "allowed_ips": _clean_list(raw.get("allowed_ips")),
            "trusted_proxies": _clean_list(raw.get("trusted_proxies")) or ["127.0.0.1", "::1"],
            "chat_mode": "direct"
            if str(raw.get("chat_mode", "astrbot") or "").strip() == "direct"
            else "astrbot",
            "vision_input": flag("vision_input", True),
            "llm_provider_id": text("llm_provider_id", "", 80),
            "default_user_id": text("default_user_id", "desktop_suzu", 80) or "desktop_suzu",
            "memory_qq": "".join(ch for ch in text("memory_qq", "", 30) if ch.isdigit()),
            "persona_id": text("persona_id", "", 80),
            "default_nickname": text("default_nickname", "小主人", 40) or "小主人",
        }



