"""提示词库：集中管理插件内全部 LLM 提示词。

只在插件 WebUI「提示词」页读写，**不出现在 AstrBot 插件配置中**；
文本落到插件数据目录的 prompt_library.json，保存后下一轮调用立即生效。
留空表示恢复该条目的内置默认文本。

模板使用 `{name}` 占位符，由 render_template 只替换已知占位符，
其余花括号（例如 JSON 示例）原样保留，便于在 WebUI 中直接编辑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger

MAX_PROMPT_CHARS = 12000

DEFAULTS: Dict[str, Dict[str, str]] = {
    # ---------------- Live2D 桌面端 ----------------
    "proactive_speech": {
        "title": "Live2D · 主动陪伴发言（第一阶段）",
        "hint": "决定这次要不要开口、说什么；想保持安静时输出空内容。格式与动作交给第二阶段。",
        "text": (
            "系统主动陪伴机会（不是用户消息）。请结合提供的屏幕参考内容、事件、好感度、"
            "当前情绪与会话，自主决定要不要说一句话；想保持安静时只输出空内容。"
            "只有提供了屏幕资料才能据此回应，不要重复打扰，一句话即可，不要解释你在做什么。\n"
        ),
    },
    "touch_reply": {
        "title": "Live2D · 模型触摸回应（第一阶段）",
        "hint": "用户触摸模型部位后的第一句话；动作与顺序由第二阶段按动作目录编排。",
        "text": (
            "用户刚刚触摸了你（桌面模型）的某个部位，这不是聊天消息，而是身体接触。"
            "请结合部位、当前好感度、当前情绪与可用动作，自然回应一句，"
            "可以惊讶、害羞、嗔怪、亲近或提醒，取决于关系与情绪。"
            "好感度越高越亲近自然，越低越克制；情绪低落或烦躁时语气收敛。"
            "不要复述好感度或情绪数值，不要解释设定，不要提到“触摸事件/部位 ID/动作名”。"
            "只输出要说的那句话；如果判断此刻不该出声，只输出空内容。\n"
        ),
    },
    "behavior_planner": {
        "title": "Live2D · 行为规划器（第二阶段）",
        "hint": "把已有的发言草稿编排成动作目录里的序列，只输出 JSON。",
        "text": (
            "你是行为计划生成器，不与人对话，也不改写别人的话。输入包含 mode、桌面能力表"
            "（白名单 + actionCatalog 动作目录）与一段已经写好的发言草稿 draft。"
            "请结合 event、好感度与当前情绪，决定要做哪些动作、按什么顺序执行，"
            "并在需要时安排发言步骤。"
            "mode=chat 时 draft 是最终回复，必须原样作为 say 步骤；mode=proactive 时可以精简 draft 或整体保持安静。"
            "can_speak=false 时不要安排 say 步骤。"
            "必须只输出一个 JSON 对象，禁止任何自然语言、解释、前后缀或 markdown 代码块。"
            '格式：{"sequenceVersion":1,"sequence":[...]}；没有合适的动作时只返回 say；'
            '完全不需要行动时输出 {"sequenceVersion":1,"sequence":[]}。'
            "动作只能使用 actionCatalog 里的 name，不要用 title 代替，名字不得自创。"
        ),
    },

    # ---------------- 人设 ----------------
    "persona_compress": {
        "title": "人设 · 压缩",
        "hint": "自我设定超出字数上限时用作压缩指令，避免直接截断丢失信息。",
        "text": (
            "你是文本压缩助手。把随后给出的“自我设定”压缩到指定字数以内，"
            "保留身份认同、语气、偏好、口头禅与相处方式，删掉重复与细节铺陈。"
            "只能输出压缩后的自我设定正文，不要解释、不要标题、不要引号、不要换行分段。"
        ),
    },
    "persona_evolution_prompt": {
        "title": "人设 · 演化",
        "hint": "占位符：{current}、{dialogue}、{max_chars}。",
        "text": """你在维护自己的“自我设定”，即可以随相处自然变化的部分：说话习惯、语气、偏好、兴趣、自称、口头禅、与人的相处方式。

当前自我设定：
{current}

最近你参与的对话（“我”代表你自己）：
{dialogue}

请判断是否需要小幅演化，返回 JSON：
{
  "changed": true 或 false,
  "persona": "演化后的自我设定（第一人称，{max_chars} 字以内，可直接注入使用）",
  "reason": "这次调整的原因（20 字以内）"
}
规则：
- 只能调整表达习惯、偏好、兴趣、口头禅、相处方式；不得改动身份、世界观、底线与安全规则
- 一次只做小幅调整，保持前后连贯；信息不足时 changed=false 并原样返回当前设定
- 不要写入具体用户的隐私或聊天原文，不要提及“演化”“设定”等元信息
只返回 JSON，不要返回其他内容。""",
    },
    "persona_evolution_system": {
        "title": "人设 · 演化（系统）",
        "hint": "人设演化的系统提示词。",
        "text": (
            "你是聊天机器人的自我设定维护器，负责根据机器人自己的聊天表现，"
            "小幅调整它可以演化的自我设定。只返回 JSON，不要返回其他内容。"
        ),
    },
    "persona_overlay_injection": {
        "title": "人设 · 自我设定注入",
        "hint": "占位符：{persona}；空设定时不会注入。",
        "text": """
<自我设定>
{persona}
</自我设定>""",
    },

    # ---------------- 情绪与好感度 ----------------
    "emotion_analysis_prompt": {
        "title": "情绪 · 分析",
        "hint": "占位符：{current_emotion}、{nickname}、{message}。",
        "text": """你是聊天机器人（角色扮演的虚拟形象），正在与用户对话。
请根据用户发给你的消息，判断：
1. 模型自身的全局情绪应该如何变化（用简短文字直接描述新的情绪状态）；
2. 你对该用户好感度的数值变化。
返回以下JSON格式：
{
  "emotion": "新的情绪状态（简短文字，如 平静、兴奋、低落、愤怒、感动、无奈 等；必须能直接代表新的情绪状态）",
  "affinity_delta": 你对该用户好感度的变化值（整数，范围 -5 到 5，0 表示无变化，正数表示好感度上升，负数表示下降）
}
判断依据：
- 用户友善、夸奖、亲近 → 情绪向积极方向变化，好感度正向变化
- 用户嘲讽、辱骂、冷漠 → 情绪向消极方向变化，好感度负向变化
- 平平无奇的日常闲聊 → 情绪保持稳定，好感度保持 0 或微小变化
只返回JSON，不要返回其他内容。

当前你的全局情绪：{current_emotion}
当前用户昵称：{nickname}
用户消息：{message}""",
    },
    "emotion_analysis_system": {
        "title": "情绪 · 分析（系统）",
        "hint": "情绪/好感度评估的系统提示词。",
        "text": (
            "你是聊天机器人的情绪/好感度评估，负责为LLM建立情绪系统，"
            "根据用户发送给你的内容决定模型的全局情绪应该如何变化（文字表述），"
            "以及对对应用户好感度的数值变化。"
            "只返回 JSON，不要返回其他内容。"
        ),
    },
    "emotion_state_injection": {
        "title": "情绪 · 关系状态注入",
        "hint": "占位符：{nickname}、{emotion}、{affinity}、{attitude}。",
        "text": """
<关系状态>
当前情绪：{emotion}
对{nickname}的好感度：{affinity}/100（{attitude}）
表达要求：把情绪和好感度体现在语气、称呼、亲近程度与聊什么上——好感度越高越自然亲近（可以开玩笑、主动关心、分享自己的事），越低越客气疏离；情绪低落/烦躁时语气相应收敛，情绪好时更活泼。不要复述或提及这些数值。
</关系状态>""",
    },

    # ---------------- 话题记忆 ----------------
    "topic_memory_analysis_prompt": {
        "title": "话题记忆 · 抽取",
        "hint": "占位符：{speaker}、{message}、{existing}、{max_ops}。",
        "text": """你在维护一个聊天场景的长期话题记忆。请根据这条新消息，决定是否要新增、更新或删除记忆。

已有记忆：
{existing}

新消息：
{speaker}：{message}

规则：
- 值得记：约定与计划、持续关注的人或事、重要事实、群体梗与称呼、明确偏好与雷点；一次性闲聊、临时情绪、猜测不记
- 每条只写一个原子事实，30 字以内，第三人称陈述（如“群里约好周末一起打副本”）
- 发言人以「昵称（QQ号）」标注；QQ 号是稳定身份，判断是否为同一人时以 QQ 号为准，昵称变化不代表换人
- 与已有记忆重复、只是补充细节 → update，并填对应 id 覆盖原条目
- 被明确指出记错、被否认、或事情已结束 → update 为最新状态或 delete 对应 id，不要保留过时内容
- 普通闲聊、没有新信息 → 返回空数组
- 一次最多 {max_ops} 条操作

返回 JSON：
{"topics":[{"action":"add|update|delete","text":"原子事实","importance":1-5,"id":"update/delete 时填已有记忆 id"}]}
只返回 JSON，不要返回其他内容。""",
    },
    "topic_memory_analysis_system": {
        "title": "话题记忆 · 抽取（系统）",
        "hint": "话题记忆抽取的系统提示词。",
        "text": (
            "你负责为聊天机器人维护某个会话（群聊或私聊）的长期记忆。"
            "只提取稳定、日后有用的事实，并以新增/更新/删除操作的形式返回。"
            "只返回 JSON，不要返回其他内容。"
        ),
    },
    "topic_memory_injection_head": {
        "title": "话题记忆 · 注入开头",
        "hint": "拼接相关话题记忆时的开头说明。",
        "text": """
<相关话题记忆>
以下是你记得的相关信息，仅作为背景参考（无需向用户复述来源或提及"记忆"）：
""",
    },
    "topic_memory_injection_owner_note": {
        "title": "话题记忆 · 群聊归属说明",
        "hint": "群聊注入时说明方括号内 QQ 号的含义。",
        "text": "方括号内是该条记忆所属群友的固定 QQ 号，昵称可能变化，判断身份时以 QQ 号为准。\n",
    },
    "topic_memory_injection_tail": {
        "title": "话题记忆 · 注入结尾",
        "hint": "拼接相关话题记忆时的结尾标签。",
        "text": "</相关话题记忆>",
    },

    # ---------------- 群聊话题跟踪（续聊） ----------------
    "topic_tracking_prompt": {
        "title": "群聊续聊 · 判定",
        "hint": "占位符：{topic}、{recent}、{nickname}、{message}。",
        "text": """当前正在进行的群聊话题（由你的上一轮发言开启）：
{topic}

最近的群聊记录：
{recent}

新消息（{nickname}）：
{message}

请判断并返回 JSON：
{
  "respond": 是否要针对这条消息发言,
  "end": 该话题是否应当结束、你不再继续参与
}
respond 的判定要像一个有分寸的群友，宁可少说也不要抢话，只有同时满足才为 true：
- 这条消息是在延续当前话题，或是在向你、向群里抛出一个你能自然接住的点
- 你说出来不会突兀，也不会显得在多嘴；群里别人之间的闲聊、已经有人接住的话，不要插话
- 不要连续抢话：如果最近几条都是你在说，或没有新信息值得回应，保持沉默
end 为 true 的条件：话题已自然收尾、对方明显转向新话题、或继续参与已不合适。
只返回 JSON，不要返回其他内容。""",
    },
    "topic_tracking_system": {
        "title": "群聊续聊 · 判定（系统）",
        "hint": "话题续聊判定的系统提示词。",
        "text": (
            "你是群聊话题跟踪判定器，负责判断新消息是否与正在进行的话题相关、"
            "是否应当续聊，以及该话题是否应当结束。只返回 JSON，不要返回其他内容。"
        ),
    },
    "topic_tracking_reply_injection": {
        "title": "群聊续聊 · 回复注入",
        "hint": "续聊回复时追加到 system_prompt 的简短说明。",
        "text": """
<话题续聊>你正在群聊中参与一个话题，请自然接续，不要重复已说过的话；若话题已自然收尾，简短回应即可。</话题续聊>""",
    },

    # ---------------- 主动发起聊天 ----------------
    "proactive_chat_system": {
        "title": "主动聊天 · 群聊（系统）",
        "hint": "与人格设定拼接使用，约束主动发言的口气。",
        "text": (
            "你是这个群里的普通群友，用手机随手打字聊天，不是客服，也不是定时播报机器人。"
            "你的话要有生活痕迹：具体、随意、有情绪，允许半句话和语气词。"
            "只输出要发到群里的那段话本身；如果此刻开口会显得突兀或打扰，只输出 [SKIP]。"
        ),
    },
    "proactive_chat_prompt": {
        "title": "主动聊天 · 群聊",
        "hint": "占位符：{time_desc}、{recent}、{last}。",
        "text": """现在是 {time_desc}。
群聊最近的记录：
{recent}

你最近在群里说过的话（不要重复这些内容或句式）：
{last}

请自然地发一条群消息，发起或延续一个轻松的话题：
- 1~2 句，像随手打出来的，不要客服腔、不要“大家好”式开场、不要总结、不要列表
- 优先顺着最近的聊天接话；如果最近没什么内容，就发起一个具体的小话题（吐槽、好奇、分享一件小事、问一个具体的小问题）
- 不要每次都在问“在不在”“在干嘛”“吃饭了吗”
- 如果现在开口会显得突兀或打扰，只输出 [SKIP]
只输出消息内容本身。""",
    },
    "private_proactive_chat_prompt": {
        "title": "主动聊天 · 私聊陪伴",
        "hint": "占位符：{time_desc}、{nickname}、{recent}、{last}。",
        "text": """现在是 {time_desc}。
你和「{nickname}」的私聊最近记录：
{recent}

你最近主动发过的消息（不要重复这些内容或句式）：
{last}

请自然地发一条私聊消息，重点是陪伴感：
- 1~2 句，像真人随手发的，可以关心对方近况、接着上次聊的话题、分享自己的一件小事
- 不要查岗、不要连续追问、不要“在吗”式开场、不要客服腔
- 如果现在发消息会打扰对方（比如对方刚说要睡了），只输出 [SKIP]
只输出消息内容本身。""",
    },

    # ---------------- 个人长期记忆 ----------------
    "personal_memory_extraction_prompt": {
        "title": "个人记忆 · 抽取",
        "hint": "占位符：{nickname}、{existing}、{message}、{max_ops}。",
        "text": """你在私聊中长期记住「{nickname}」。请根据这条新消息，决定是否要新增、更新或删除记忆。

已有记忆：
{existing}

新消息：
{nickname}：{message}

规则：
- 只记稳定、长期有用的事实：身份/称呼、喜好与厌恶、重要经历、长期计划、和机器人的关系梗；一次性寒暄、临时情绪、猜测出来的内容不要记
- 每条只写一个原子事实，20 字以内，第三人称陈述（如“喜欢喝冰美式”“在准备考研”）
- 与已有记忆重复、只是补充细节 → update，并填对应 id 覆盖原条目
- 用户明确指出某条记忆不对、要求更正（如“记错了”“不是…是…”“别记这个”）→ 必须处理：能确定正确内容就 update 对应 id 为正确事实，只是要求删掉就 delete 对应 id；绝不能把错误条目留着
- 明显过时或被否认 → delete，并填对应 id
- 普通闲聊、没有新信息 → 返回空数组
- 一次最多 {max_ops} 条操作

返回 JSON：
{"memories":[{"action":"add|update|delete","category":"profile|preference|event|plan|relation","text":"原子事实","importance":1-5,"id":"update/delete 时填已有记忆 id"}]}
只返回 JSON，不要返回其他内容。""",
    },
    "personal_memory_extraction_system": {
        "title": "个人记忆 · 抽取（系统）",
        "hint": "个人长期记忆抽取的系统提示词。",
        "text": (
            "你负责为聊天机器人维护一位私聊用户的长期个人记忆。"
            "只提取稳定、可复用的事实，并以新增/更新/删除操作的形式返回。只返回 JSON，不要返回其他内容。"
        ),
    },
    "personal_memory_injection_head": {
        "title": "个人记忆 · 注入开头",
        "hint": "占位符：{nickname}。",
        "text": "\n<关于{nickname}的长期记忆>\n（背景参考，自然使用即可，不要逐条复述，也不要提“记忆库”）",
    },

    # ---------------- 表情包 ----------------
    "meme_placeholder_injection": {
        "title": "表情包 · 占位符注入",
        "hint": "占位符：{placeholder_tag}；引导主模型按需输出表情包占位符。",
        "text": """
如需用表情包增强回复，可输出 [{placeholder_tag}:情绪]（每轮最多 1 个，情绪∈开心/难过/惊讶/无语/愤怒/困惑/害羞/搞笑），非必要不用。""",
    },
    "meme_description_prompt": {
        "title": "表情包 · 图片描述",
        "hint": "用于自动识别表情包并生成描述与标签，要求只返回 JSON。",
        "text": """请分析这张表情包图片，返回以下JSON格式：
{
  "description": "简短描述图片内容（20字以内）",
  "tags": ["标签1", "标签2", "标签3"],
  "emotion": "情绪关键词（从以下选择：happy/sad/surprised/angry/confused/shy/funny/speechless）"
}
只返回JSON，不要返回其他内容。""",
    },
    "meme_description_system": {
        "title": "表情包 · 图片描述（系统）",
        "hint": "表情包图片分析的系统提示词。",
        "text": "你是一个图片分析助手，专门分析表情包图片并返回结构化的JSON描述。",
    },
}


def default_prompt(key: str) -> str:
    """取某条提示词的内置默认文本（未知键返回空串）。"""
    return str((DEFAULTS.get(key) or {}).get("text") or "")


def render_template(template: str, **values: Any) -> str:
    """只替换模板中的已知占位符，其余花括号原样保留。"""
    text = template or ""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


class PromptLibrary:
    """提示词库的读取、校验与持久化；实例化后作为当前生效库。"""

    _active: Optional["PromptLibrary"] = None

    def __init__(self, plugin: Any):
        self.path = Path(str(getattr(plugin, "plugin_data_path", "") or ".")) / "prompt_library.json"
        self.data: Dict[str, str] = self.load()
        PromptLibrary._active = self

    def load(self) -> Dict[str, str]:
        """读取提示词文件，缺失或损坏时回退默认文本。"""
        raw: Dict[str, Any] = {}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = loaded
            except Exception as e:
                logger.warning(f"[Fiscok's][prompt] 读取提示词库失败，使用默认值: {e}")
        return self.normalize(raw)

    def save(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """保存外部提交的提示词；空文本或未知键会被归一为默认值。"""
        raw = dict(payload or {})
        self.data = self.normalize({**self.data, **raw})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            logger.info("[Fiscok's][prompt] 提示词库已更新")
        except Exception as e:
            logger.warning(f"[Fiscok's][prompt] 保存提示词库失败: {e}")
        return self.data

    def get(self, key: str) -> str:
        """取生效文本；未配置或为空时回退内置默认。"""
        return self.data.get(key) or default_prompt(key)

    def view(self) -> List[Dict[str, Any]]:
        """WebUI 展示用：含标题、说明、当前文本与内置默认文本。"""
        return [
            {
                "key": key,
                "title": meta["title"],
                "hint": meta["hint"],
                "text": self.get(key),
                "default": meta["text"],
            }
            for key, meta in DEFAULTS.items()
        ]

    @staticmethod
    def normalize(raw: Dict[str, Any]) -> Dict[str, str]:
        """只保留已知键并限制长度；空白文本恢复默认。"""
        raw = raw if isinstance(raw, dict) else {}
        data: Dict[str, str] = {}
        for key, meta in DEFAULTS.items():
            value = raw.get(key)
            text = str(value if value is not None else "")[:MAX_PROMPT_CHARS]
            data[key] = text if text.strip() else meta["text"]
        return data


def active_library() -> Optional[PromptLibrary]:
    """当前生效的提示词库；插件未初始化时为 None。"""
    return PromptLibrary._active


def get_prompt(key: str) -> str:
    """供各业务模块读取提示词（无库时回退内置默认）。"""
    lib = PromptLibrary._active
    return lib.get(key) if lib is not None else default_prompt(key)


def render_prompt(key: str, **values: Any) -> str:
    """读取并渲染提示词模板。"""
    return render_template(get_prompt(key), **values)
