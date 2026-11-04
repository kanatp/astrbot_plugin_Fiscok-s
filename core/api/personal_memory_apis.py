"""
个人长期记忆（仅私聊）相关能力：
- 用辅助 LLM 把私聊内容抽取成“原子事实”并给出新增/更新/删除操作
- 按用户分文件存储，超过上限时淘汰低价值条目，控制规模
- 请求时按词向量只检索少量相关记忆注入 system_prompt，控制上下文开销
"""
import asyncio
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.core.star.context import Context

from ..prompts import (
    PERSONAL_MEMORY_CATEGORY_LABELS,
    PERSONAL_MEMORY_EXTRACTION_SYSTEM_PROMPT,
    format_personal_memory_extraction_prompt,
)
from .provider_utils import get_aux_provider
from .emotion_apis import _extract_json
from .topic_memory_apis import _cosine

# 记忆抽取温度（偏低，保证分类/动作稳定）
MEMORY_EXTRACTION_TEMPERATURE = 0.3

# 允许的记忆类别
VALID_MEMORY_CATEGORIES = set(PERSONAL_MEMORY_CATEGORY_LABELS.keys())

# 允许的记忆操作
VALID_MEMORY_ACTIONS = {'add', 'update', 'delete'}


def format_existing_memories(memories: List[Dict], limit: int = 20, max_chars: int = 30) -> str:
    """
    把已有记忆压缩成给模型看的清单（只带 id/类别/内容），便于模型决定 update/delete
    """
    lines = []
    for memory in memories[-limit:]:
        text = str(memory.get('text', '')).strip()
        if not text:
            continue
        label = PERSONAL_MEMORY_CATEGORY_LABELS.get(str(memory.get('category', '')), '记录')
        if len(text) > max_chars:
            text = text[:max_chars] + '…'
        lines.append(f"- id={memory.get('id', '')} [{label}] {text}")
    return "\n".join(lines)


async def extract_personal_memories(
    context: Context,
    provider_id: str,
    nickname: str,
    message: str,
    existing: List[Dict],
    max_ops: int = 3,
    timeout: float = 20.0,
) -> List[Dict]:
    """
    调用辅助 LLM 从一条私聊消息中抽取记忆操作。

    :return: 操作列表，每项形如
             {"action": "add|update|delete", "category": "profile|preference|event|plan|relation",
              "text": str, "importance": int, "id": str}
             无需记忆或失败时返回空列表
    """
    if not message or not message.strip():
        return []

    try:
        provider = await get_aux_provider(context, provider_id)
        if provider is None:
            logger.warning("[Fiscok's][memory] 未找到可用的 LLM Provider，跳过个人记忆抽取")
            return []

        prompt = format_personal_memory_extraction_prompt(
            nickname=nickname,
            existing=format_existing_memories(existing),
            message=message,
            max_ops=max_ops,
        )
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt=PERSONAL_MEMORY_EXTRACTION_SYSTEM_PROMPT,
                temperature=MEMORY_EXTRACTION_TEMPERATURE,
            ),
            timeout=timeout,
        )
        if not response or not response.completion_text:
            return []

        result = _extract_json(response.completion_text)
        if not isinstance(result, dict):
            return []

        operations = []
        for item in result.get('memories', [])[:max_ops]:
            if not isinstance(item, dict):
                continue
            action = str(item.get('action', '')).strip().lower()
            if action not in VALID_MEMORY_ACTIONS:
                continue
            category = str(item.get('category', '')).strip().lower()
            if category not in VALID_MEMORY_CATEGORIES:
                category = 'profile'
            text = str(item.get('text', '')).strip()
            if action in ('add', 'update') and not text:
                continue
            try:
                importance = max(1, min(5, int(item.get('importance', 3) or 3)))
            except (TypeError, ValueError):
                importance = 3
            operations.append({
                'action': action,
                'category': category,
                'text': text,
                'importance': importance,
                'id': str(item.get('id', '') or '').strip(),
            })
        return operations
    except asyncio.TimeoutError:
        logger.warning("[Fiscok's][memory] 个人记忆抽取超时，跳过")
        return []
    except Exception as e:
        logger.warning(f"[Fiscok's][memory] 个人记忆抽取失败，不影响主流程: {e}")
        return []


def find_similar_memory(
    memories: List[Dict],
    vector: List[float],
    threshold: float = 0.88,
) -> Optional[Dict]:
    """
    在已有记忆中找与给定向量高度相似的一条（用于去重/合并）
    """
    if not vector:
        return None
    best = None
    best_sim = threshold
    for memory in memories:
        sim = _cosine(vector, memory.get('vector') or [])
        if sim is not None and sim >= best_sim:
            best = memory
            best_sim = sim
    return best


def retrieve_memories(
    memories: List[Dict],
    query_vector: List[float],
    top_k: int = 4,
    min_similarity: float = 0.45,
) -> List[Dict]:
    """
    按词向量检索相关个人记忆；重要度作为轻微加权，命中后按相关度降序返回
    """
    if not query_vector or not memories:
        return []

    scored = []
    dim_mismatch = 0
    for memory in memories:
        sim = _cosine(query_vector, memory.get('vector') or [])
        if sim is None:
            dim_mismatch += 1
            continue
        if sim < min_similarity:
            continue
        importance = int(memory.get('importance', 3) or 3)
        scored.append({'memory': memory, 'score': sim + 0.01 * importance})

    if dim_mismatch:
        logger.warning(f"[Fiscok's][memory] 有 {dim_mismatch} 条个人记忆向量维度不一致，已跳过（可能是更换了 Embedding Provider）")

    scored.sort(key=lambda x: x['score'], reverse=True)
    return [item['memory'] for item in scored[:max(1, top_k)]]
