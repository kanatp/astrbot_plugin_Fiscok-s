'''
用于调用 LLM 对表情包图片进行描述和标签生成
'''
import json
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context
from ..prompts import MEME_DESCRIPTION_PROMPT, MEME_DESCRIPTION_SYSTEM_PROMPT, EMOTION_MAP, VALID_EMOTIONS


async def generate_meme_description(
    image_path: str,
    context: Context,
    provider_id: str = ""
) -> Optional[Dict]:
    """
    调用 LLM 对表情包图片进行描述和标签生成

    Args:
        image_path: 图片本地路径
        context: AstrBot Context 实例
        provider_id: LLM Provider ID，留空则使用默认

    Returns:
        {"description": "...", "tags": [...], "emotion": "..."} 或 None
    """
    try:
        # 获取 Provider 实例
        provider = context.get_provider_by_id(provider_id) if provider_id else None

        if provider is None:
            # 如果未指定或未找到，尝试获取默认 Provider
            all_providers = context.get_all_providers()
            if all_providers:
                provider = all_providers[0]
            else:
                logger.error("[meme_apis] 未找到可用的 LLM Provider")
                return None

        logger.info(f"[meme_apis] 正在使用 Provider {provider_id or 'default'} 生成表情包描述")

        # 调用 LLM 进行图片描述
        response = await provider.text_chat(
            prompt=MEME_DESCRIPTION_PROMPT,
            image_urls=[image_path],
            system_prompt=MEME_DESCRIPTION_SYSTEM_PROMPT
        )

        if not response or not response.completion_text:
            logger.warning("[meme_apis] LLM 返回为空")
            return None

        # 解析 JSON 响应
        result_text = response.completion_text.strip()

        # 剥离 LLM 特殊标记（如 <|begin_of_box|>...<|end_of_box|>）
        import re
        result_text = re.sub(r'<\|[^|]+\|>', '', result_text).strip()

        # 尝试提取 JSON 部分（处理可能的 markdown 代码块）
        if result_text.startswith("```"):
            # 移除 markdown 代码块标记
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

        result = json.loads(result_text)

        # 验证返回格式
        if not all(key in result for key in ["description", "tags", "emotion"]):
            logger.warning(f"[meme_apis] LLM 返回格式不完整: {result}")
            return None

        # 验证 emotion 是否在允许范围内
        if result["emotion"] not in VALID_EMOTIONS:
            # 尝试映射
            result["emotion"] = EMOTION_MAP.get(result["emotion"], "funny")

        logger.info(f"[meme_apis] 成功生成表情包描述: {result}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"[meme_apis] 解析 LLM 返回的 JSON 失败: {e}, 原文: {response.completion_text if response else 'None'}")
        return None
    except Exception as e:
        logger.error(f"[meme_apis] 生成表情包描述失败: {e}", exc_info=True)
        return None
