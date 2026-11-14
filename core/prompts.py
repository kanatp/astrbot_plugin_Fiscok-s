'''
提示词管理模块
提示词正文统一存放在 prompt_library（可在插件 WebUI「提示词」页修改），
这里只保留格式化函数与少量固定映射。
'''

from .prompt_library import get_prompt, render_prompt


# ==================== 表情包相关 ====================

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
    """格式化表情包占位符注入提示词"""
    return render_prompt("meme_placeholder_injection", placeholder_tag=placeholder_tag)


# ==================== 人设图与生图相关 ====================

def format_persona_image_injection(placeholder_tag: str, image_list: str) -> str:
    """格式化人设图占位符注入提示词（带可用图片清单）"""
    return render_prompt(
        "persona_image_injection",
        placeholder_tag=placeholder_tag,
        image_list=image_list,
    )


def format_image_gen_injection(placeholder_tag: str, max_per_turn: int = 1) -> str:
    """格式化生图占位符注入提示词"""
    return render_prompt(
        "image_gen_injection",
        placeholder_tag=placeholder_tag,
        max_per_turn=max_per_turn,
    )


# ==================== 情绪与好感度相关提示词 ====================

def _attitude_label(affinity: int) -> str:
    """把好感度数值映射为关系描述，帮助模型理解程度"""
    if affinity >= 80:
        return '很亲近'
    if affinity >= 60:
        return '比较熟悉'
    if affinity >= 40:
        return '普通'
    if affinity >= 20:
        return '有点疏远'
    return '明显疏远'


def format_emotion_analysis_prompt(current_emotion: str, nickname: str, message: str) -> str:
    """格式化情绪/好感度分析提示词"""
    return render_prompt(
        "emotion_analysis_prompt",
        current_emotion=current_emotion,
        nickname=nickname,
        message=message,
    )


def format_emotion_state_injection(nickname: str, emotion: str, affinity: int) -> str:
    """格式化情绪/好感度状态注入提示词"""
    return render_prompt(
        "emotion_state_injection",
        nickname=nickname,
        emotion=emotion,
        affinity=affinity,
        attitude=_attitude_label(affinity),
    )


# ==================== 人设演化（自我设定）相关提示词 ====================

def format_persona_evolution_prompt(current: str, dialogue: str, max_chars: int) -> str:
    """格式化人设演化提示词"""
    return render_prompt(
        "persona_evolution_prompt",
        current=current or "（暂无，可在本次对话中形成）",
        dialogue=dialogue or "（无）",
        max_chars=max_chars,
    )


def format_persona_overlay_injection(persona: str) -> str:
    """格式化自我设定注入文本（空设定返回空字符串）"""
    persona = (persona or "").strip()
    if not persona:
        return ""
    return render_prompt("persona_overlay_injection", persona=persona)


# ==================== 话题记忆相关提示词 ====================

def format_topic_memory_analysis_prompt(
    nickname: str, dialogue: str, existing: str = '', max_ops: int = 3, sender_id: str = ''
) -> str:
    """格式化话题记忆判定提示词"""
    speaker = f"{nickname}（QQ{sender_id}）" if sender_id else nickname
    return render_prompt(
        "topic_memory_analysis_prompt",
        speaker=speaker,
        message=dialogue,
        existing=existing or "（无）",
        max_ops=max_ops,
    )


def format_topic_memory_injection(topics, show_owner: bool = False) -> str:
    """将检索到的话题记忆拼装为注入文本"""
    items = []
    has_owner = False
    for topic in topics:
        text = str(topic.get("text", "")).strip()
        if not text:
            continue
        owner_id = str(topic.get("owner_id", "") or "").strip()
        if show_owner and owner_id:
            items.append(f"- [QQ{owner_id}] {text}")
            has_owner = True
        else:
            items.append(f"- {text}")
    if not items:
        return ""
    head = get_prompt("topic_memory_injection_head")
    if has_owner:
        head += get_prompt("topic_memory_injection_owner_note")
    tail = get_prompt("topic_memory_injection_tail")
    return head + "\n".join(items) + "\n" + tail


# ==================== 群聊话题跟踪（续聊）相关提示词 ====================

def format_topic_tracking_prompt(topic: str, recent: str, nickname: str, message: str) -> str:
    """格式化话题续聊判定提示词"""
    return render_prompt(
        "topic_tracking_prompt",
        topic=topic or "（无）",
        recent=recent or "（无）",
        nickname=nickname,
        message=message,
    )


# ==================== 主动发起聊天（冷场救场）相关提示词 ====================

def format_proactive_chat_prompt(time_desc: str, recent: str, last: str) -> str:
    """格式化主动发起聊天提示词"""
    return render_prompt(
        "proactive_chat_prompt",
        time_desc=time_desc or "某个时间",
        recent=recent or "（无）",
        last=last or "（无）",
    )


def format_private_proactive_chat_prompt(time_desc: str, nickname: str, recent: str, last: str) -> str:
    """格式化私聊主动发起（陪伴向）提示词"""
    return render_prompt(
        "private_proactive_chat_prompt",
        time_desc=time_desc or "某个时间",
        nickname=nickname or "对方",
        recent=recent or "（无）",
        last=last or "（无）",
    )


# ==================== 个人长期记忆（私聊）相关提示词 ====================

# 记忆类别 -> 中文标签（注入与展示时使用）
PERSONAL_MEMORY_CATEGORY_LABELS = {
    "profile": "档案",
    "preference": "偏好",
    "event": "经历",
    "plan": "计划",
    "relation": "关系",
}


def format_personal_memory_extraction_prompt(
    nickname: str,
    existing: str,
    message: str,
    max_ops: int = 3,
) -> str:
    """格式化个人记忆抽取提示词"""
    return render_prompt(
        "personal_memory_extraction_prompt",
        nickname=nickname or "对方",
        existing=existing or "（无）",
        message=message or "（无）",
        max_ops=max(1, int(max_ops)),
    )


def format_personal_memory_injection(nickname: str, memories) -> str:
    """将检索到的个人记忆拼装为注入文本（每行一条，压缩体积）"""
    items = []
    for memory in memories:
        text = str(memory.get('text', '')).strip()
        if not text:
            continue
        label = PERSONAL_MEMORY_CATEGORY_LABELS.get(str(memory.get('category', '')), '记录')
        items.append(f"- {label}：{text}")
    if not items:
        return ""
    head = render_prompt("personal_memory_injection_head", nickname=nickname or "对方")
    return head + "\n" + "\n".join(items) + "\n</关于长期记忆>"
