"""
拟真打字延迟服务 (TypingService)

改进点：
- 废弃机械死板的固定间隔（0.5s），采用符合人类打字心理的动态延迟算法：
  Delay = Base + (CharCount * CharRate) + Jitter
  短句（如“好呀！”）延迟 0.5~0.8s；长句（如 40 字）延迟 2.0~3.0s；
- 设有最大延时保护（默认上限 4.0s），防止长文本等待过久；
- 尝试在支持的协议端（如 NapCat / Lagrange）发送 set_input_status 设置“正在输入中...”，极大增加活人在场感。
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

from astrbot.api import logger


class TypingService:
    @staticmethod
    def calculate_delay(
        text: str,
        base: float = 0.4,
        char_rate: float = 0.045,
        jitter_range: float = 0.25,
        max_delay: float = 4.0,
    ) -> float:
        """
        计算拟真打字耗时（秒）
        """
        clean_len = len((text or "").strip())
        if clean_len == 0:
            return 0.1

        # 基础延时 + 每个字符打字耗时 + 随机扰动
        jitter = random.uniform(-jitter_range, jitter_range)
        computed = base + (clean_len * char_rate) + jitter
        # 范围限制：至少 0.3s，最高不超过 max_delay
        return max(0.3, min(max_delay, computed))

    @staticmethod
    async def try_set_input_status(event: Any) -> None:
        """
        尝试调用协议端 set_input_status 接口设置“正在输入中...”
        （协议端不支持时静默忽略）
        """
        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            if not api or not hasattr(api, "call_action"):
                return

            group_id = event.get_group_id() if hasattr(event, "get_group_id") else ""
            sender_id = event.get_sender_id() if hasattr(event, "get_sender_id") else ""

            if group_id:
                # 群聊输入状态
                if str(group_id).isdigit():
                    await api.call_action("set_input_status", group_id=int(group_id), status_type=1)
            elif sender_id and str(sender_id).isdigit():
                # 私聊输入状态
                await api.call_action("set_input_status", user_id=int(sender_id), status_type=1)
        except Exception:
            pass
