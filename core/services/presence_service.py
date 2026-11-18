"""
跨端在场协同服务 (PresenceService)

实现 QQ 私聊与 Live2D 桌面端在场感知协同：
- 当用户在电脑前敲代码、与桌面端交互时（Live2D 同步活跃或 busy 状态），标记为“PC 在场”；
- 此时 QQ 私聊的主动找话题/冷场救场自动回避，避免手机端弹出冗余打扰；
- 当桌面端长时间静默，转为移动端或离线状态，允许私聊温和关怀。
"""
from __future__ import annotations

import time
from typing import Any, Dict


class PresenceService:
    def __init__(self):
        # user_id -> {"last_desktop_active": float, "is_busy": bool}
        self._desktop_presence: Dict[str, Dict[str, Any]] = {}
        # user_id -> last_qq_active float
        self._qq_presence: Dict[str, float] = {}

    def record_desktop_active(self, user_id: str, is_busy: bool = False) -> None:
        """记录桌面端活跃（心跳/聊天/同步）"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        self._desktop_presence[user_id] = {
            "last_active": time.time(),
            "is_busy": is_busy,
        }

    def record_qq_active(self, user_id: str) -> None:
        """记录 QQ 端发言活跃"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        self._qq_presence[user_id] = time.time()

    def is_desktop_active(self, user_id: str, threshold_seconds: float = 300.0) -> bool:
        """判断用户近期是否正在使用桌面端（默认 5 分钟内）"""
        user_id = str(user_id or "").strip()
        state = self._desktop_presence.get(user_id)
        if not state:
            return False
        return (time.time() - state.get("last_active", 0)) <= threshold_seconds

    def should_suppress_qq_proactive(self, user_id: str) -> bool:
        """
        判断是否应当抑制 QQ 私聊的主动搭话：
        如果用户此刻就在电脑前活跃（或正在忙碌），抑制手机 QQ 的主动打扰
        """
        user_id = str(user_id or "").strip()
        state = self._desktop_presence.get(user_id)
        if not state:
            return False
        now = time.time()
        last_active = state.get("last_active", 0)
        # 5 分钟内桌面有动作，或者桌面处于 busy 状态
        if (now - last_active < 300.0) or state.get("is_busy"):
            return True
        return False
