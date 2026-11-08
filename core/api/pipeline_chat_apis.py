"""把外部消息（Live2D 桌面端）注入 AstrBot 事件队列，复用主 Agent 管线。

桥接只负责发消息与收回文本，人格、工具、技能、插件钩子、上下文管理等
全部交给 AstrBot 自己处理，避免桌面端另起一套 prompt 注入。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import List

from astrbot.api import logger
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.platform.sources.webchat.webchat_event import WebChatMessageEvent
from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr

# 这些回包不属于用户可见的回复，直接忽略
_IGNORED_TYPES = {
    "run_started",
    "agent_stats",
    "follow_up_captured",
    "break",
    "audio_chunk",
}
_IGNORED_CHAIN_TYPES = {"tool_call", "tool_call_result", "agent_stats", "reasoning", "break"}


def _build_message(
    text: str,
    user_id: str,
    nickname: str,
    self_id: str,
    request_id: str,
    image_base64: str = "",
) -> AstrBotMessage:
    """构造一条私聊消息对象（字段必须填齐，事件构造时会读取发送者与消息概要）"""
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = self_id
    message.session_id = str(user_id)
    message.message_id = request_id
    message.sender = MessageMember(str(user_id), nickname)
    message.message_str = text
    message.message = [Plain(text)]
    if image_base64:
        message.message.append(Image.fromBase64(image_base64))
    message.raw_message = {
        "post_type": "message",
        "message_type": "private",
        "user_id": str(user_id),
        "raw_message": text,
    }
    return message


async def chat_via_astrbot(
    context,
    *,
    text: str,
    user_id: str,
    nickname: str,
    platform_id: str,
    platform_name: str,
    self_id: str = "",
    environment: str = "",
    image_base64: str = "",
    first_timeout: float = 90.0,
    idle_timeout: float = 10.0,
) -> tuple:
    """注入一条事件并等待管线回包，返回 (最终文本, 是否携带图片)。

    platform_id 决定 umo 的第一段，传入真实 QQ 平台实例 id 且 user_id 为绑定 QQ 时，
    umo 与 QQ 私聊会话完全一致，配置、人格、历史与钩子全部对齐。
    platform_name 固定用注入名，避免被当成 QQ 消息做合并转发等额外装饰。
    """
    request_id = f"suzu-{uuid.uuid4()}"
    message = _build_message(
        text, user_id, nickname, self_id or "0", request_id, image_base64
    )
    platform_meta = PlatformMetadata(
        name=platform_name,
        description="Live2D 桌面端",
        id=platform_id,
    )
    event = WebChatMessageEvent(text, message, platform_meta, str(user_id))
    event.set_extra("_suzu_pipeline", True)
    if environment:
        event.set_extra("_suzu_environment", environment)

    back_queue = webchat_queue_mgr.get_or_create_back_queue(
        request_id, f"suzu:{user_id}"
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + first_timeout
    stream_text = ""
    segments: List[str] = []
    got_content = False

    await context.get_event_queue().put(event)
    try:
        while True:
            wait = idle_timeout if got_content else max(0.1, deadline - loop.time())
            try:
                item = await asyncio.wait_for(back_queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                if got_content:
                    break
                raise RuntimeError("AstrBot 管线响应超时")
            if not isinstance(item, dict):
                continue

            msg_type = str(item.get("type") or "")
            chain_type = str(item.get("chain_type") or "")
            data = item.get("data")
            if msg_type == "end":
                break
            if msg_type in _IGNORED_TYPES:
                continue
            if msg_type == "complete":
                if not isinstance(data, str) or not data.strip():
                    continue
                if item.get("streaming"):
                    # 流式的 complete 携带完整文本，覆盖已累加的增量
                    stream_text = data
                else:
                    segments.append(data)
                got_content = True
                continue
            if msg_type != "plain" or chain_type in _IGNORED_CHAIN_TYPES:
                continue
            if not isinstance(data, str) or not data.strip():
                continue
            if item.get("streaming"):
                stream_text += data
            else:
                # 非流式：同一条消息可能被管线拆成多段，按行拼接
                segments.append(data)
            got_content = True
    finally:
        webchat_queue_mgr.remove_back_queue(request_id)

    reply = stream_text.strip() if stream_text.strip() else "\n".join(segments).strip()
    if not reply:
        raise RuntimeError(
            "AstrBot 管线没有返回文本（可能未唤醒：请检查私聊唤醒前缀、"
            "会话白名单或 Provider 唤醒前缀）"
        )
    logger.debug(
        f"[Fiscok's][suzu] 管线回复完成 "
        f"(umo={platform_id}:FriendMessage:{user_id}, 字符数={len(reply)}, "
        f"图片={'有' if image_base64 else '无'})"
    )
    return reply, bool(image_base64)
