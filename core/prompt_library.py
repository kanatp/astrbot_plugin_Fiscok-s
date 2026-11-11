"""提示词库：集中管理桌面端行为相关提示词。

只在插件 WebUI「提示词」页读写，**不出现在 AstrBot 插件配置中**；
文本落到插件数据目录的 prompt_library.json，保存后下一轮调用立即生效。
留空表示恢复该条目的内置默认文本。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from astrbot.api import logger

MAX_PROMPT_CHARS = 8000

DEFAULTS: Dict[str, Dict[str, str]] = {
    "proactive_speech": {
        "title": "主动陪伴发言（第一阶段）",
        "hint": "决定这次要不要开口、说什么；想保持安静时输出空内容。格式与动作交给第二阶段。",
        "text": (
            "系统主动陪伴机会（不是用户消息）。请结合提供的屏幕参考内容、事件、好感度、"
            "当前情绪与会话，自主决定要不要说一句话；想保持安静时只输出空内容。"
            "只有提供了屏幕资料才能据此回应，不要重复打扰，一句话即可，不要解释你在做什么。\n"
        ),
    },
    "touch_reply": {
        "title": "模型触摸回应（第一阶段）",
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
        "title": "行为规划器（第二阶段）",
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
    "persona_compress": {
        "title": "人设压缩",
        "hint": "自我设定超出字数上限时用作压缩指令，避免直接截断丢失信息。",
        "text": (
            "你是文本压缩助手。把随后给出的“自我设定”压缩到指定字数以内，"
            "保留身份认同、语气、偏好、口头禅与相处方式，删掉重复与细节铺陈。"
            "只能输出压缩后的自我设定正文，不要解释、不要标题、不要引号、不要换行分段。"
        ),
    },
}


def default_prompt(key: str) -> str:
    """取某条提示词的内置默认文本（未知键返回空串）。"""
    return str((DEFAULTS.get(key) or {}).get("text") or "")


class PromptLibrary:
    """提示词库的读取、校验与持久化。"""

    def __init__(self, plugin: Any):
        self.path = Path(str(getattr(plugin, "plugin_data_path", "") or ".")) / "prompt_library.json"
        self.data: Dict[str, str] = self.load()

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
        """只保留已知键并限制长度；空文本恢复默认。"""
        raw = raw if isinstance(raw, dict) else {}
        data: Dict[str, str] = {}
        for key, meta in DEFAULTS.items():
            value = raw.get(key)
            text = str(value or "").strip()[:MAX_PROMPT_CHARS]
            data[key] = text or meta["text"]
        return data
