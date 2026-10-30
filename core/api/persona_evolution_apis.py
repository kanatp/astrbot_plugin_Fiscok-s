'''人设演化：让模型在长期相处中小幅修订自己的“自我设定”（语气、偏好、口头禅、相处方式）。
注意：这里不改 AstrBot 的全局 persona，而是生成一份插件侧可注入、可回滚的自我设定文本。'''
import asyncio
import json
import re
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context

from ..prompts import (
    PERSONA_EVOLUTION_SYSTEM_PROMPT,
    format_persona_evolution_prompt,
)
from .provider_utils import get_aux_provider

# 人设演化使用偏高的温度，允许措辞与偏好上有一点变化
PERSONA_EVOLUTION_TEMPERATURE = 1.0


def _extract_json(result_text: str) -> Optional[dict]:
    """从 LLM 返回文本中提取 JSON 对象（兼容 markdown 代码块与前后多余文字）"""
    text = (result_text or '').strip()
    text = re.sub(r'<\|[^|]+\|>', '', text).strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(line for line in lines if not line.strip().startswith('```')).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


async def evolve_persona(
    context: Context,
    provider_id: str = '',
    current_persona: str = '',
    dialogue: str = '',
    max_chars: int = 200,
    timeout: float = 25.0,
) -> Optional[Dict]:
    """
    让模型基于近期对话小幅演化自我设定

    :return: {"changed": bool, "persona": str, "reason": str}；失败/超时返回 None
    """
    try:
        provider = await get_aux_provider(context, provider_id)
        if provider is None:
            logger.warning("[Fiscok's][persona] 未找到可用的 LLM Provider，跳过人设演化")
            return None

        prompt = format_persona_evolution_prompt(current_persona, dialogue, max_chars)
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt=PERSONA_EVOLUTION_SYSTEM_PROMPT,
                temperature=PERSONA_EVOLUTION_TEMPERATURE,
            ),
            timeout=timeout,
        )
        if not response or not response.completion_text:
            return None

        data = _extract_json(response.completion_text)
        if not isinstance(data, dict):
            logger.warning("[Fiscok's][persona] 人设演化返回内容无法解析为 JSON，跳过")
            return None

        persona = str(data.get('persona', '') or '').strip()
        if not persona:
            return None
        if max_chars > 0 and len(persona) > max_chars:
            persona = persona[:max_chars]

        changed = bool(data.get('changed', False))
        # 文本完全没变视为未变化，避免无意义的重复写入
        if persona == (current_persona or '').strip():
            changed = False

        return {
            'changed': changed,
            'persona': persona,
            'reason': str(data.get('reason', '') or '').strip()[:50],
        }
    except asyncio.TimeoutError:
        logger.warning("[Fiscok's][persona] 人设演化超时，跳过")
        return None
    except Exception as e:
        logger.warning(f"[Fiscok's][persona] 人设演化失败，跳过: {e}")
        return None
