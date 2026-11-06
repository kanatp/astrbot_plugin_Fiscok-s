'''
话题记忆（词向量检索）相关能力：
- 复用 AstrBot 内置 EmbeddingProvider 计算词向量（未配置时功能静默禁用）
- 调用关闭思考的辅助 LLM 判定是否保存话题，并抽取“唤起词 + 话题内容”
- 纯 Python 余弦相似度检索话题库
'''
import asyncio
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context
from astrbot.core.provider.provider import EmbeddingProvider

from ..prompts import (
    TOPIC_MEMORY_ANALYSIS_SYSTEM_PROMPT,
    format_topic_memory_analysis_prompt,
)
from .provider_utils import get_aux_provider
from .emotion_apis import _extract_json

# 辅助 LLM 判定话题的温度（偏低，保证判定稳定）
TOPIC_DECISION_TEMPERATURE = 0.3


def resolve_embedding_provider(context: Context, provider_id: str = "") -> Optional[EmbeddingProvider]:
    """
    解析用于词向量的 Embedding Provider。

    优先使用指定 ID；未指定或未命中时回退到第一个可用的 Embedding Provider；
    都没有则返回 None（话题记忆功能静默禁用）。
    """
    if provider_id:
        provider = context.get_provider_by_id(provider_id)
        if isinstance(provider, EmbeddingProvider):
            return provider
        logger.warning(f"[Fiscok's][topic] 指定的 Provider {provider_id} 不是 Embedding Provider，回退到默认")

    providers = context.get_all_embedding_providers()
    return providers[0] if providers else None


async def get_text_embedding(
    context: Context,
    provider_id: str,
    text: str,
    timeout: float = 10.0,
) -> Optional[List[float]]:
    """
    计算文本的词向量（带超时保护，失败/超时返回 None，不影响主流程）
    """
    if not text or not text.strip():
        return None

    provider = resolve_embedding_provider(context, provider_id)
    if provider is None:
        return None

    try:
        return await asyncio.wait_for(provider.get_embedding(text), timeout=timeout)
    except Exception as e:
        logger.warning(f"[Fiscok's][topic] 计算词向量失败: {e}")
        return None


async def extract_topic_memories(
    context: Context,
    llm_provider_id: str,
    nickname: str,
    message: str,
    existing: List[Dict],
    max_ops: int = 3,
    timeout: float = 20.0,
    sender_id: str = '',
) -> List[Dict]:
    """
    调用辅助 LLM，把这条消息转成针对某个会话记忆库的新增/更新/删除操作。

    :param existing: 该会话已有的记忆条目（含 id/text/owner_id）
    :param sender_id: 发言人 QQ 号（稳定身份标识，昵称变化不影响归属）
    :return: 操作列表，每项 {"action": add|update|delete, "text": str, "importance": int, "id": str}
    """
    if not message or not message.strip():
        return []
    try:
        max_ops = max(1, int(max_ops or 3))
    except (TypeError, ValueError):
        max_ops = 3

    try:
        provider = await get_aux_provider(context, llm_provider_id)
        if provider is None:
            logger.warning("[Fiscok's][topic] 未找到可用的 LLM Provider，跳过话题记忆抽取")
            return []

        lines = []
        for item in existing[-30:]:
            text = str(item.get('text', '')).strip()
            if not text:
                continue
            owner_id = str(item.get('owner_id', '') or '').strip()
            prefix = f"[QQ{owner_id}] " if owner_id else ''
            lines.append(f"- id={item.get('id', '')}：{prefix}{text[:60]}")

        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=format_topic_memory_analysis_prompt(
                    nickname, message, "\n".join(lines), max_ops, sender_id
                ),
                system_prompt=TOPIC_MEMORY_ANALYSIS_SYSTEM_PROMPT,
                temperature=TOPIC_DECISION_TEMPERATURE,
            ),
            timeout=timeout,
        )
        if not response or not response.completion_text:
            return []

        result = _extract_json(response.completion_text)
        if not result:
            return []

        raw_ops = result.get('topics')
        if not isinstance(raw_ops, list):
            return []

        operations = []
        for raw in raw_ops:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get('action', '')).strip().lower()
            if action not in ('add', 'update', 'delete'):
                continue
            text = str(raw.get('text', '')).strip()
            op_id = str(raw.get('id', '')).strip()
            if action in ('add', 'update') and not text:
                continue
            if action == 'update' and not op_id:
                action = 'add'
            if action == 'delete' and not op_id:
                continue
            try:
                importance = int(raw.get('importance', 3) or 3)
            except (TypeError, ValueError):
                importance = 3
            operations.append({
                'action': action,
                'text': text,
                'importance': max(1, min(5, importance)),
                'id': op_id,
            })
        if operations:
            logger.info(f"[Fiscok's][topic] 记忆操作: {[(op['action'], op['text'][:20]) for op in operations]}")
        return operations[:max(1, max_ops)]
    except asyncio.TimeoutError:
        logger.warning("[Fiscok's][topic] 话题记忆抽取超时，跳过")
        return []
    except Exception as e:
        logger.warning(f"[Fiscok's][topic] 话题记忆抽取失败，不影响主流程: {e}")
        return []


def _cosine(a: List[float], b: List[float]) -> Optional[float]:
    """纯 Python 余弦相似度；向量为空或维度不一致时返回 None"""
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return None
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


def retrieve_topics(
    topics: List[Dict],
    query_vector: List[float],
    top_k: int = 3,
    min_similarity: float = 0.5,
) -> List[Dict]:
    """
    按词向量相似度从话题库中检索相关内容。

    :param topics: 话题库条目列表（每项含 keyword/content/vector）
    :param query_vector: 查询向量
    :param top_k: 最多返回条数
    :param min_similarity: 相似度下限，低于该值不返回
    :return: 按相似度降序排列的话题列表
    """
    if not query_vector or not topics:
        return []

    scored = []
    dim_mismatch = 0
    for topic in topics:
        sim = _cosine(query_vector, topic.get("vector") or [])
        if sim is None:
            dim_mismatch += 1
            continue
        if sim >= min_similarity:
            text = str(topic.get("text", "")).strip()
            if not text:
                continue
            scored.append({
                "text": text,
                "owner_id": str(topic.get("owner_id", "") or ""),
                "owner_name": str(topic.get("owner_name", "") or ""),
                "similarity": sim,
            })

    if dim_mismatch:
        logger.warning(f"[Fiscok's][topic] 有 {dim_mismatch} 条话题向量维度不一致，已跳过（可能是更换了 Embedding Provider）")

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]