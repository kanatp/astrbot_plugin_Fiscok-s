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
