"""
连续消息合并服务 (MergeService)

修复与增强：
1. 状态键隔离：使用 (umo, sender_id) 作为键，彻底解决群聊中不同成员同时发言导致的消息互相覆盖丢失问题。
2. 双重同步：合并完成时，不仅同步更新 event.message_str，同时重构更新 event.message_obj.message 中的 Plain 组件链，
   确保下游所有插件与核心均看到完整合并内容。
3. 可靠拦截：前序消息等待期间若检测到更新的消息到达，立即触发 event.stop_event()，杜绝重复回复。
4. 内存自清理：自动清理超时的孤立片段，防止内存泄露。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.message_components import Plain


class MergeService:
    def __init__(self):
        # (umo, sender_id) -> {'fragments': List[str], 'last_id': Any, 'last_time': float}
        self._pending: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._cleanup_counter = 0

    def _periodic_cleanup(self):
        """定期清理超时的孤立记录（超过 60 秒未更新）"""
        self._cleanup_counter += 1
        if self._cleanup_counter < 50:
            return
        self._cleanup_counter = 0
        now = time.time()
        expired_keys = [
            k for k, v in self._pending.items()
            if now - v.get("last_time", 0) > 60
        ]
        for k in expired_keys:
            self._pending.pop(k, None)

    async def handle_message(self, event: Any, config: Dict[str, Any]) -> bool:
        """
        处理单条消息的合并逻辑。
        :return: True 表示当前消息应该继续流转；False 表示当前消息已被合并/截断，调用方无需继续处理。
        """
        # 桌面端与绑定 QQ 共用 umo，跳过合并，避免和 QQ 私聊片段串在一起
        if event.get_extra("_suzu_pipeline"):
            return True

        if not config.get("available", False):
            return True

        is_group = bool(event.get_group_id()) if hasattr(event, "get_group_id") else False
        if config.get("private_only", True) and is_group:
            return True

        sender_id = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
        self_id = event.get_self_id() if hasattr(event, "get_self_id") else ""
        if not sender_id or sender_id == self_id:
            return True

        message = (getattr(event, "message_str", "") or "").strip()
        # 忽略空消息与斜杠指令
        if not message or message.startswith(("/", "／")):
            return True

        umo = getattr(event, "unified_msg_origin", "")
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None) or id(event)
        key = (umo, sender_id)

        self._periodic_cleanup()

        pending = self._pending.get(key)
        if pending is None:
            pending = {
                "fragments": [],
                "last_id": None,
                "last_time": time.time(),
            }
            self._pending[key] = pending

        pending["fragments"].append(message)
        max_fragments = max(1, int(config.get("max_fragments", 4) or 4))
        if len(pending["fragments"]) > max_fragments:
            del pending["fragments"][: len(pending["fragments"]) - max_fragments]
        pending["last_id"] = message_id
        pending["last_time"] = time.time()

        delay = max(0.0, float(config.get("debounce_seconds", 1.2) or 0))
        if delay > 0:
            await asyncio.sleep(delay)

        # 等待期间又来了同一说话人的新消息：这一条消息合并入后续消息，当前消息立即停止流转
        if pending["last_id"] != message_id:
            if hasattr(event, "stop_event"):
                event.stop_event()
            return False

        # 如果当前记录已经被消费或者当前就是唯一一条，正常流转
        fragments = pending["fragments"][:]
        pending["fragments"] = []
        self._pending.pop(key, None)

        if len(fragments) <= 1:
            return True

        # 合并多条片段
        merged_text = "\n".join(fragments)
        event.message_str = merged_text

        # 同步重构 event.message_obj.message 组件链
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj and hasattr(msg_obj, "message") and isinstance(msg_obj.message, list):
            new_components = []
            plain_replaced = False
            for comp in msg_obj.message:
                if isinstance(comp, Plain):
                    if not plain_replaced:
                        comp.text = merged_text
                        new_components.append(comp)
                        plain_replaced = True
                    # 丢弃后续多余的 Plain
                else:
                    new_components.append(comp)
            if not plain_replaced:
                new_components.insert(0, Plain(merged_text))
            msg_obj.message = new_components

        logger.info(
            f"[MergeService] 已成功合并 {len(fragments)} 条连续消息 (来自: {sender_id} @ {umo})"
        )
        return True
