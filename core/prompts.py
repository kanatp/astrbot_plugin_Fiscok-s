'''
提示词管理模块
集中管理所有 LLM 相关的提示词模板
'''


# ==================== 表情包相关提示词 ====================

# 表情包占位符注入提示词（添加到 system_prompt 中引导 LLM 生成占位符）
# 使用时需要格式化 placeholder_tag 参数
MEME_PLACEHOLDER_INJECTION = """

如果需要使用表情包来增强回复效果，可以在回复中使用占位符 [{placeholder_tag}:情绪描述]，
例如 [{placeholder_tag}:开心]、[{placeholder_tag}:无语]、[{placeholder_tag}:困惑]。
可用的情绪关键词：开心、难过、惊讶、无语、愤怒、困惑、害羞、搞笑。
注意：每次回复最多使用一个表情包占位符，且只在合适的时候使用。"""

# 表情包图片描述生成提示词
MEME_DESCRIPTION_PROMPT = """请分析这张表情包图片，返回以下JSON格式：
{
  "description": "简短描述图片内容（20字以内）",
  "tags": ["标签1", "标签2", "标签3"],
  "emotion": "情绪关键词（从以下选择：happy/sad/surprised/angry/confused/shy/funny/speechless）"
}
只返回JSON，不要返回其他内容。"""

# 表情包描述生成的系统提示词
MEME_DESCRIPTION_SYSTEM_PROMPT = "你是一个图片分析助手，专门分析表情包图片并返回结构化的JSON描述。"

# 情绪关键词映射（中文 -> 英文）
EMOTION_MAP = {
    "开心": "happy", "高兴": "happy", "快乐": "happy",
    "难过": "sad", "悲伤": "sad", "伤心": "sad",
    "惊讶": "surprised", "震惊": "surprised", "惊": "surprised",
    "愤怒": "angry", "生气": "angry", "怒": "angry",
    "困惑": "confused", "疑惑": "confused", "懵": "confused",
    "害羞": "shy", "羞": "shy",
    "搞笑": "funny", "滑稽": "funny", "笑": "funny",
    "无语": "speechless", "沉默": "speechless",
}

# 有效的情绪英文关键词列表
VALID_EMOTIONS = ["happy", "sad", "surprised", "angry", "confused", "shy", "funny", "speechless"]


def format_meme_placeholder_injection(placeholder_tag: str = "meme") -> str:
    """
    格式化表情包占位符注入提示词

    Args:
        placeholder_tag: 占位符标签名

    Returns:
        格式化后的提示词
    """
    return MEME_PLACEHOLDER_INJECTION.format(placeholder_tag=placeholder_tag)


# ==================== 情绪与好感度相关提示词 ====================

# 情绪/好感度分析的系统提示词
# 注意：情绪是模型自身的全局状态（文字表述，运行时变量）；好感度是模型对每个用户的态度（数值，持久化）
EMOTION_ANALYSIS_SYSTEM_PROMPT = ("你是聊天机器人的情绪/好感度评估，负责为LLM建立情绪系统，"
                                  "根据用户发送给你的内容决定模型的全局情绪应该如何变化（文字表述），"
                                  "以及对对应用户好感度的数值变化。"
                                  "只返回 JSON，不要返回其他内容。")

# 情绪/好感度分析提示词（使用时需要格式化 current_emotion、nickname、message）
EMOTION_ANALYSIS_PROMPT = """你是聊天机器人（角色扮演的虚拟形象），正在与用户对话。
请根据用户发给你的消息，判断：
1. 模型自身的全局情绪应该如何变化（用简短文字直接描述新的情绪状态）；
2. 你对该用户好感度的数值变化。
返回以下JSON格式：
{{
  "emotion": "新的情绪状态（简短文字，如 平静、兴奋、低落、愤怒、感动、无奈 等；必须能直接代表新的情绪状态）",
  "affinity_delta": 你对该用户好感度的变化值（整数，范围 -5 到 5，0 表示无变化，正数表示好感度上升，负数表示下降）
}}
判断依据：
- 用户友善、夸奖、亲近 → 情绪向积极方向变化，好感度正向变化
- 用户嘲讽、辱骂、冷漠 → 情绪向消极方向变化，好感度负向变化
- 平平无奇的日常闲聊 → 情绪保持稳定，好感度保持 0 或微小变化
只返回JSON，不要返回其他内容。

当前你的全局情绪：{current_emotion}
当前用户昵称：{nickname}
用户消息：{message}"""

# 情绪/好感度状态注入提示词（添加到主 LLM 的 system_prompt 中）
# 使用时需要格式化 nickname、emotion、affinity
EMOTION_STATE_INJECTION = """
<当前状态>
你当前的全局情绪：{emotion}
你对该用户（{nickname}）的好感度：{affinity}
</当前状态>
请根据你的全局情绪调整回复的整体语气：情绪积极则欢快热情，情绪消极则低沉疏远。
请根据对该用户的好感度调整对待该用户的态度：好感度越高越亲切热情，越低越冷淡疏离。"""


def format_emotion_analysis_prompt(current_emotion: str, nickname: str, message: str) -> str:
    """
    格式化情绪/好感度分析提示词

    Args:
        current_emotion: 模型当前的全局情绪（文字描述）
        nickname: 用户昵称
        message: 用户消息内容

    Returns:
        格式化后的提示词
    """
    return EMOTION_ANALYSIS_PROMPT.format(
        current_emotion=current_emotion,
        nickname=nickname,
        message=message,
    )


def format_emotion_state_injection(nickname: str, emotion: str, affinity: int) -> str:
    """
    格式化情绪/好感度状态注入提示词

    Args:
        nickname: 用户昵称
        emotion: 模型当前的全局情绪（文字描述）
        affinity: 模型对该用户的当前好感度

    Returns:
        格式化后的提示词
    """
    return EMOTION_STATE_INJECTION.format(nickname=nickname, emotion=emotion, affinity=affinity)


# ==================== 话题记忆相关提示词 ====================

# 话题记忆判定的系统提示词
TOPIC_MEMORY_ANALYSIS_SYSTEM_PROMPT = ("你是一个聊天机器人的长期话题记忆维护器，"
                                       "负责判断当前对话中是否出现了值得长期记住的话题，"
                                       "并在需要时抽取唤起词与该话题的原始对话内容。"
                                       "只返回 JSON，不要返回其他内容。")

# 话题记忆判定提示词（使用时需要格式化 nickname、dialogue）
TOPIC_MEMORY_ANALYSIS_PROMPT = """你是聊天机器人的长期话题记忆维护器，正在与用户对话。
请判断当前这段对话中，是否出现了值得长期记住的话题（例如用户的偏好、约定、重要事实、持续关注的事件等）。

返回以下JSON格式：
{{
  "save": true 或 false（是否需要保存这个话题）,
  "keyword": "唤起词（简短、便于日后唤起该话题，10字以内）",
  "content": "话题记忆（忠实还原该话题的原始对话片段或要点）"
}}
判断依据：
- 值得长期记住：用户的稳定偏好、约定、重要事实、持续关注的人/事/物 → save=true
- 一次性寒暄、临时性闲聊、无明确话题、已在记忆中充分覆盖的内容 → save=false
- save=false 时 keyword 与 content 返回空字符串
只返回JSON，不要返回其他内容。

当前用户昵称：{nickname}
当前这段对话：
{dialogue}"""

# 话题记忆注入提示词（添加到主 LLM 的 system_prompt 中，由 format_topic_memory_injection 拼装）
TOPIC_MEMORY_INJECTION_HEAD = """
<相关话题记忆>
以下是你过去记住的相关话题，仅作为背景参考（无需向用户复述来源或提及"记忆"）：
"""

TOPIC_MEMORY_INJECTION_TAIL = "</相关话题记忆>"


def format_topic_memory_analysis_prompt(nickname: str, dialogue: str) -> str:
    """
    格式化话题记忆判定提示词

    Args:
        nickname: 用户昵称
        dialogue: 当前这段对话（原始对话文本）

    Returns:
        格式化后的提示词
    """
    return TOPIC_MEMORY_ANALYSIS_PROMPT.format(nickname=nickname, dialogue=dialogue)


def format_topic_memory_injection(topics) -> str:
    """
    将检索到的话题记忆拼装为注入文本

    Args:
        topics: 话题列表，每项为 {"keyword": str, "content": str}

    Returns:
        注入文本（无有效话题时返回空字符串）
    """
    items = []
    for topic in topics:
        keyword = str(topic.get("keyword", "")).strip()
        content = str(topic.get("content", "")).strip()
        if not content:
            continue
        items.append(f"- 唤起词：{keyword}\n  话题内容：{content}")
    if not items:
        return ""
    return TOPIC_MEMORY_INJECTION_HEAD + "\n".join(items) + "\n" + TOPIC_MEMORY_INJECTION_TAIL
