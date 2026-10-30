'''
群聊话题跟踪（续聊）相关能力：
- 调用关闭思考的辅助 LLM，判定新消息是否应接续话题、以及话题是否应当结束
- 低温度、只返回 JSON；失败或超时返回 None，不影响主流程
'''
import asyncio
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context

from ..prompts import TOPIC_TRACKING_SYSTEM_PROMPT, format_topic_tracking_prompt
from .provider_utils import get_aux_provider
from .emotion_apis import _extract_json

# 续聊判定温度（偏低，保证判定稳定）
TOPIC_TRACKING_TEMPERATURE = 0.2


def _to_bool(value, default: bool = False) -> bool:
    """兼容模型返回字符串/数字形式的布尔值"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', '是')
    if isinstance(value, (int, float)):
        return bool(value)
    return default


async def decide_topic_continuation(
    context: Context,
    provider_id: str,
    nickname: str,
    message: str,
    topic: str,
    recent: List[str],
    timeout: float = 15.0,
) -> Optional[Dict]:
    """
    判定当前消息是否应续聊当前话题。

    :param timeout: 单次判定超时（秒），避免阻塞消息处理流水线
    :return: {"respond": bool, "end": bool}；判定失败返回 None
    """
    if not message or not message.strip():
        return None

    try:
        provider = await get_aux_provider(context, provider_id)
        if provider is None:
            logger.warning("[Fiscok's][track] 未找到可用的 LLM Provider，跳过话题续聊判定")
            return None

        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=format_topic_tracking_prompt(
                    topic,
                    "\n".join(recent) if recent else "（无）",
                    nickname,
                    message,
                ),
                system_prompt=TOPIC_TRACKING_SYSTEM_PROMPT,
                temperature=TOPIC_TRACKING_TEMPERATURE,
            ),
            timeout=timeout,
        )
        if not response or not response.completion_text:
            return None

        result = _extract_json(response.completion_text)
        if not result:
            return None

        return {
            "respond": _to_bool(result.get("respond"), False),
            "end": _to_bool(result.get("end"), False),
        }
    except Exception as e:
        logger.warning(f"[Fiscok's][track] 话题续聊判定失败，不影响主流程: {e}")
        return None
