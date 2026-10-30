import asyncio
from typing import List, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context

from ..prompts import (
    PROACTIVE_CHAT_SYSTEM_PROMPT,
    format_proactive_chat_prompt,
    format_private_proactive_chat_prompt,
)
from .provider_utils import get_aux_provider

# 主动发起聊天（冷场救场）：
# - 结合当前时段、群聊最近记录、自己最近说过的话与人格设定，生成一条像真人随手发的群消息
# - 模型可返回 [SKIP] 表示此刻不适合开口（读空气），避免机械式定时发言
# - 失败或超时返回 None，不影响主流程

# 主动发言温度偏高，保证每次措辞不一样
PROACTIVE_CHAT_TEMPERATURE = 0.9

# 模型判断此刻不该开口时输出的标记
PROACTIVE_SKIP_TOKEN = '[SKIP]'


def _clean_output(text: str) -> str:
    """清理模型输出：去引号、去“我：”前缀等"""
    text = (text or '').strip()
    if not text:
        return ''
    for left, right in (('"', '"'), ('“', '”')):
        if text.startswith(left) and text.endswith(right) and len(text) >= 2:
            text = text[1:-1].strip()
    for prefix in ('我：', '我:', '消息：', '消息:'):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


async def generate_proactive_message(
    context: Context,
    provider_id: str,
    persona_prompt: str,
    time_desc: str,
    recent: List[str],
    last_messages: List[str],
    nickname: str = '',
    is_private: bool = False,
    timeout: float = 20.0,
) -> Optional[str]:
    """生成一条主动消息（群聊冷场救场或私聊陪伴）；模型决定不开口、生成失败或超时返回 None"""
    try:
        provider = await get_aux_provider(context, provider_id)
        if provider is None:
            logger.warning("[Fiscok's][proactive] 未找到可用的 LLM Provider，跳过主动发言")
            return None

        system_prompt = PROACTIVE_CHAT_SYSTEM_PROMPT
        if persona_prompt and persona_prompt.strip():
            system_prompt = f"{persona_prompt.strip()}\n\n{system_prompt}"

        if is_private:
            prompt = format_private_proactive_chat_prompt(
                time_desc,
                nickname,
                "\n".join(recent) if recent else "（无）",
                "\n".join(last_messages) if last_messages else "（无）",
            )
        else:
            prompt = format_proactive_chat_prompt(
                time_desc,
                "\n".join(recent) if recent else "（无）",
                "\n".join(last_messages) if last_messages else "（无）",
            )

        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=PROACTIVE_CHAT_TEMPERATURE,
            ),
            timeout=timeout,
        )
        if not response or not response.completion_text:
            return None

        raw = response.completion_text.strip()
        if PROACTIVE_SKIP_TOKEN in raw.upper():
            logger.info("[Fiscok's][proactive] 模型判定此刻不适合开口（SKIP）")
            return None

        return _clean_output(raw) or None
    except asyncio.TimeoutError:
        logger.warning("[Fiscok's][proactive] 主动发言生成超时，跳过")
        return None
    except Exception as e:
        logger.warning(f"[Fiscok's][proactive] 主动发言生成失败，跳过: {e}")
        return None
