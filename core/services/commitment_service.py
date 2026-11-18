"""
待办约定与情感关怀追踪服务 (CommitmentTrackerService) - Phase 2 深度增强版

核心机制：
1. 语义时间与事件类型解析：自动识别“明天下午”、“后天”、“今晚”、“周末”等时间窗口，
   并分类为 event（日程事件）、health（身心健康）、mood（情绪心绪）。
2. 双时态关怀（前置打气 / 节点跟进 / 后置回响）：
   到期窗口触发时，在私聊对话与主动陪伴提示词中注入自然关怀备忘，提示模型温和问候。
3. 状态闭环（fulfilled/expired）：触发并回响后自动标记闭环，防止反复复读。
4. 增删查改指令支持：提供查看、新增、删除与清空接口。
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger


class CommitmentTrackerService:
    def __init__(self, data_dir: Path):
        self.file_path = Path(data_dir) / "commitments.json"
        # user_id -> List[Dict]
        self._commitments: Dict[str, List[Dict[str, Any]]] = self._load()

    def _load(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[CommitmentTracker] 加载约定关怀库失败: {e}")
            return {}

    def _save(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._commitments, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"[CommitmentTracker] 保存约定关怀库失败: {e}")
            return False

    @staticmethod
    def infer_time_and_type(text: str) -> Tuple[float, str, str]:
        """
        从文本中智能推断目标时间戳、关怀类型与回响引导
        :return: (target_timestamp, concern_type, follow_up_hint)
        """
        now = datetime.now()
        target_dt = now + timedelta(hours=12)  # 默认半天后
        concern_type = "event"
        follow_up_hint = text

        # 1. 语义时间推断
        if "后天" in text:
            target_dt = (now + timedelta(days=2)).replace(hour=16, minute=0, second=0)
        elif "明天" in text:
            if any(k in text for k in ("下午", "晚", "傍晚")):
                target_dt = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0)
            elif any(k in text for k in ("上午", "早")):
                target_dt = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0)
            else:
                target_dt = (now + timedelta(days=1)).replace(hour=15, minute=0, second=0)
        elif any(k in text for k in ("今晚", "晚上")):
            target_dt = now.replace(hour=21, minute=30, second=0)
            if target_dt < now:
                target_dt = now + timedelta(hours=2)
        elif any(k in text for k in ("下周", "周一", "星期一")):
            days_ahead = (7 - now.weekday()) % 7 or 7
            target_dt = (now + timedelta(days=days_ahead)).replace(hour=10, minute=0, second=0)
        elif any(k in text for k in ("周末", "周六", "星期六")):
            days_ahead = (5 - now.weekday()) % 7 or 7
            target_dt = (now + timedelta(days=days_ahead)).replace(hour=14, minute=0, second=0)

        # 2. 关怀分类识别
        if any(k in text for k in ("感冒", "发烧", "头疼", "头晕", "胃疼", "生病", "吃药", "去医院", "挂号", "输液", "手术", "拔牙")):
            concern_type = "health"
            follow_up_hint = "关心身体是否好些了、提醒多喝温水或按时吃药"
        elif any(k in text for k in ("难受", "难过", "伤心", "压力大", "被批评", "被骂", "失眠", "emo", "好累", "疲惫")):
            concern_type = "mood"
            follow_up_hint = "给予温柔包容的陪伴，倾听对方并送上安慰与拥抱"
        elif any(k in text for k in ("答辩", "考试", "面试", "交初稿", "开题", "汇报", "开会")):
            concern_type = "event"
            follow_up_hint = "询问事情进展如何、辛苦了一天适当放松"
        else:
            concern_type = "event"
            follow_up_hint = "自然回响并适度询问进展"

        return target_dt.timestamp(), concern_type, follow_up_hint

    def add_commitment(
        self,
        user_id: str,
        topic: str,
        target_timestamp: float = 0.0,
        concern_type: str = "",
        follow_up_hint: str = "",
    ) -> Optional[str]:
        """
        添加一条待关怀事项。若未传入时间或类型，将自动从文本语义推断。
        """
        user_id = str(user_id or "").strip()
        topic = str(topic or "").strip()
        if not user_id or not topic:
            return None

        # 自动推断
        inferred_time, inferred_type, inferred_hint = self.infer_time_and_type(topic)
        final_time = target_timestamp if target_timestamp > 0 else inferred_time
        final_type = concern_type if concern_type else inferred_type
        final_hint = follow_up_hint if follow_up_hint else inferred_hint

        now = time.time()
        item_id = f"care_{int(now)}_{random.randint(100, 999)}"
        items = self._commitments.setdefault(user_id, [])

        item = {
            "id": item_id,
            "topic": topic,
            "target_time": final_time,
            "concern_type": final_type,
            "follow_up_hint": final_hint,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "fulfilled_at": None,
        }
        items.append(item)
        if len(items) > 30:
            self._commitments[user_id] = items[-30:]

        self._save()
        logger.info(
            f"[CommitmentTracker] 已记录对用户 {user_id} 的关怀 [{final_type}]: {topic} "
            f"(预计回响: {datetime.fromtimestamp(final_time).strftime('%Y-%m-%d %H:%M')})"
        )
        return item_id

    def get_due_commitments(self, user_id: str, max_items: int = 2) -> List[Dict[str, Any]]:
        """
        获取当前已到期且待关怀的事项（在到期前 30 分钟至到期后 48 小时内）
        """
        user_id = str(user_id or "").strip()
        items = self._commitments.get(user_id) or []
        now = time.time()

        due = []
        for item in items:
            if item.get("status") != "pending":
                continue
            target_time = float(item.get("target_time") or 0)
            if (target_time - 1800 <= now <= target_time + 48 * 3600):
                due.append(item)
            elif now > target_time + 48 * 3600:
                item["status"] = "expired"

        return due[:max_items]

    def mark_fulfilled(self, user_id: str, item_id: str) -> bool:
        """标记某条事项已被回响处理"""
        user_id = str(user_id or "").strip()
        items = self._commitments.get(user_id) or []
        for item in items:
            if item.get("id") == item_id and item.get("status") == "pending":
                item["status"] = "fulfilled"
                item["fulfilled_at"] = datetime.now().isoformat()
                self._save()
                logger.info(f"[CommitmentTracker] 关怀事项已回响闭环: {item.get('topic')}")
                return True
        return False

    def delete_commitment(self, user_id: str, item_id: str) -> bool:
        """删除指定关怀事项"""
        user_id = str(user_id or "").strip()
        items = self._commitments.get(user_id) or []
        for i, item in enumerate(items):
            if item.get("id") == item_id:
                items.pop(i)
                self._save()
                return True
        return False

    def clear_commitments(self, user_id: str) -> bool:
        """清空指定用户的关怀库"""
        user_id = str(user_id or "").strip()
        if user_id in self._commitments:
            self._commitments[user_id] = []
            self._save()
            return True
        return False

    def get_all_commitments(self, user_id: str = "") -> List[Dict[str, Any]]:
        """获取指定用户的所有关怀记录（未指定时返回全部）"""
        if user_id:
            return list(self._commitments.get(user_id, []))
        all_items = []
        for items in self._commitments.values():
            all_items.extend(items)
        return all_items

    def format_care_injection(self, due_items: List[Dict[str, Any]], nickname: str) -> str:
        """格式化关怀提示词注入文本，包含精细分类标签与引导"""
        if not due_items:
            return ""
        type_labels = {
            "health": "【身心健康呵护】",
            "mood": "【情绪心绪关怀】",
            "event": "【重要约定跟进】",
        }
        lines = []
        for item in due_items:
            t_type = item.get("concern_type", "event")
            tag = type_labels.get(t_type, "【近期关怀】")
            topic = str(item.get("topic") or "").strip()
            hint = str(item.get("follow_up_hint") or "").strip()
            lines.append(f"- {tag} {topic}（建议引导：{hint}）")

        formatted_items = "\n".join(lines)
        try:
            from ..prompt_library import render_prompt
            return render_prompt(
                "care_reminder_template",
                nickname=nickname,
                formatted_items=formatted_items,
            )
        except Exception:
            return (
                f"\n<近期关怀备忘>\n"
                f"你一直把「{nickname}」挂念在心上。对方之前提及过以下事情，现在正值非常适合关怀的回响时机：\n"
                f"{formatted_items}\n"
                f"表达要求：体现出“一直在默默惦记你”的陪伴感，自然而贴心地问候对方现在的状况（如身体是否好些了、事情进展如何）；"
                f"不要像面试官一样生硬追问，不要提及“根据记忆/备忘录”。\n"
                f"</近期关怀备忘>"
            )
