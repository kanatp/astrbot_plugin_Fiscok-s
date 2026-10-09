'''
用于调用 LLM（高温度设置）分析模型的全局情绪变化与对用户的好感度变化
注意：情绪是模型自身的全局状态（文字表述，运行时变量）；好感度是模型对每个用户的态度（数值）
'''
import json
import re
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context
from ..prompts import EMOTION_ANALYSIS_SYSTEM_PROMPT, format_emotion_analysis_prompt
from .provider_utils import get_aux_provider


def _extract_json(result_text: str) -> Optional[dict]:
    """
    从 LLM 返回文本中提取 JSON（剥离特殊标记、处理 markdown 代码块）

    Args:
        result_text: LLM 返回的原始文本

    Returns:
        解析出的 JSON dict，失败返回 None
    """
    try:
        result_text = result_text.strip()
        # 剥离 LLM 特殊标记（如 <|begin_of_box|>...<|end_of_box|>）
        result_text = re.sub(r'<\|[^|]+\|>', '', result_text).strip()

        # 尝试提取 JSON 部分（处理可能的 markdown 代码块）
        if result_text.startswith("```"):
            lines = result_text.split("\n")
            json_lines = []
            in_code_block = False
            for line in lines:
                if line.startswith("```") and not in_code_block:
                    in_code_block = True
                    continue
                elif line.startswith("```") and in_code_block:
                    break
                elif in_code_block:
                    json_lines.append(line)
            result_text = "\n".join(json_lines)

        return json.loads(result_text)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"[emotion_apis] 解析 LLM 返回的 JSON 失败: {e}, 原文: {result_text[:200]}")
        return None


async def analyze_emotion_state(
    context: Context,
    provider_id: str = "",
    nickname: str = "",
    message: str = "",
    current_emotion: str = "平静",
    temperature: float = 1.2,
    delta_max: int = 5,
) -> Optional[Dict]:
    """
    调用 LLM（高温度）分析模型的全局情绪变化（文字）与对用户的好感度变化（数值）

    Args:
        context: AstrBot Context 实例
        provider_id: LLM Provider ID，留空则使用默认
        nickname: 用户昵称
        message: 用户消息内容
        current_emotion: 模型当前的全局情绪（文字描述）
        temperature: 模型温度（高温度设置）
        delta_max: 好感度单次变化绝对值上限

    Returns:
        {"emotion": str, "affinity_delta": int} 或 None
    """
    try:
        # 获取关闭思考的独立 Provider 实例（未配置时回退到第一个可用模型）
        provider = await get_aux_provider(context, provider_id)

        if provider is None:
            logger.error("[emotion_apis] 未找到可用的 LLM Provider")
            return None

        logger.info(f"[emotion_apis] 正在使用 Provider {provider_id or 'default'} 分析用户 {nickname} 的情绪/好感度")

        # 调用 LLM 进行情绪/好感度分析
        response = await provider.text_chat(
            prompt=format_emotion_analysis_prompt(current_emotion, nickname, message),
            system_prompt=EMOTION_ANALYSIS_SYSTEM_PROMPT,
            temperature=temperature,
        )

        if not response or not response.completion_text:
            logger.warning("[emotion_apis] LLM 返回为空")
            return None

        result = _extract_json(response.completion_text)

        if not result:
            return None

        # 情绪：文字表述的新状态
        new_emotion = str(result.get("emotion", "")).strip()
        if not new_emotion:
            logger.warning(f"[emotion_apis] LLM 未返回新的情绪状态: {result}")
            return None

        # 好感度变化：数值校验并截断到 [-delta_max, delta_max]
        if "affinity_delta" not in result:
            logger.warning(f"[emotion_apis] LLM 返回格式不完整: {result}")
            return None

        try:
            affinity_delta = int(result["affinity_delta"])
        except (TypeError, ValueError):
            logger.warning(f"[emotion_apis] LLM 返回的 affinity_delta 不是整数: {result}")
            return None

        affinity_delta = max(-delta_max, min(delta_max, affinity_delta))

        logger.info(f"[emotion_apis] 情绪变化: {current_emotion} -> {new_emotion}, affinity_delta={affinity_delta}")
        return {
            "emotion": new_emotion,
            "affinity_delta": affinity_delta,
        }

    except Exception as e:
        logger.error(f"[emotion_apis] 分析情绪/好感度失败: {e}", exc_info=True)
        return None
