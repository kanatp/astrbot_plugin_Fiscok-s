"""
主动聊天服务 (ProactiveService) - 包含模型自主夜间日程生成引擎 (AI Autonomous Life Journal)

核心机制：
1. 状态持久化落盘：每日发言次数、冷却时间、上次发言时间持久化存储至 proactive_states.json，
   Bot 重启或热重载不会重置次数，彻底杜绝重启后突兀打扰。
2. 双端在场联动：集成 PresenceService，当检测到用户在 PC 前（Live2D 活跃或 busy）时，自动抑制手机 QQ 私聊主动打扰。
3. 惦念与关怀联动：集成 CommitmentTrackerService，当有到期的事件/约定备忘时，自动注入主动提示词中，
   让主动搭话直击心灵（“答辩怎么样啦/感冒好些了吗”）。
4. 模型自主日常生活日程引擎 (AI Autonomous Life Journal)：
   - 每夜（或日期更替时）由模型结合星期几、周末/节假日、人设及近期对话话题，自主规划全天 24 小时 8 个时段的生活日程；
   - 结果持久化至 bot_daily_life.json，彻底摆脱死板静态池；
   - 带有平滑安全兜底：模型未生成或异常时无缝回退至拟真情景池。
5. 用户作息画像追踪 (User Circadian Profile)：
   记录用户活跃时段，自动识别“夜猫子”、“早起党”或“常规作息”。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from ..api.provider_utils import get_aux_provider

# 标准 8 个时段区间划分
_SCHEDULE_SLOTS = [
    {"start": 0, "end": 6, "period": "深夜/凌晨"},
    {"start": 6, "end": 9, "period": "清晨"},
    {"start": 9, "end": 12, "period": "上午"},
    {"start": 12, "end": 14, "period": "午间"},
    {"start": 14, "end": 17, "period": "下午"},
    {"start": 17, "end": 19, "period": "傍晚"},
    {"start": 19, "end": 22, "period": "晚间"},
    {"start": 22, "end": 24, "period": "深夜"},
]


class ProactiveService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.file_path = self.data_dir / "proactive_states.json"
        self.life_file_path = self.data_dir / "bot_daily_life.json"

        # 整体存储结构：{"states": {umo: ...}, "user_habits": {uid: {"night": 0, "day": 0, "early": 0}}}
        raw_data = self._load()
        if "states" in raw_data and isinstance(raw_data["states"], dict):
            self._states: Dict[str, Dict[str, Any]] = raw_data["states"]
            self._user_habits: Dict[str, Dict[str, int]] = raw_data.get("user_habits", {})
        else:
            self._states = raw_data
            self._user_habits = {}

        # 加载今日自主日常生活日程
        self._daily_life: Dict[str, Any] = self._load_daily_life()
        self._is_generating_life: bool = False

    def _load(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[ProactiveService] 加载主动发言状态库失败: {e}")
            return {}

    def _save(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "states": self._states,
                "user_habits": self._user_habits,
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"[ProactiveService] 保存主动发言状态库失败: {e}")
            return False

    def _load_daily_life(self) -> Dict[str, Any]:
        if not self.life_file_path.exists():
            return {}
        try:
            with open(self.life_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[ProactiveService] 加载机器人日常生活日程失败: {e}")
            return {}

    def _save_daily_life(self) -> bool:
        try:
            self.life_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.life_file_path, "w", encoding="utf-8") as f:
                json.dump(self._daily_life, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"[ProactiveService] 保存机器人日常生活日程失败: {e}")
            return False

    # ---------------- 机器人模型自主日常生活日程引擎 ----------------

    def get_bot_current_activity(self, hour: int = -1) -> str:
        """
        获取机器人当前时刻正在进行的自主虚拟生活日常。
        优先读取模型自主生成的今日 24 小时日程；若未生成或异常，平滑回退到确定性情景池。
        """
        lt = time.localtime()
        if hour < 0:
            hour = lt.tm_hour

        today_str = time.strftime("%Y-%m-%d", lt)

        # 检查是否有手动覆盖
        if self._daily_life.get("date") == today_str and self._daily_life.get("custom_override"):
            return str(self._daily_life["custom_override"])

        # 检查今日自主生成的日程
        if self._daily_life.get("date") == today_str:
            schedule = self._daily_life.get("schedule") or []
            for item in schedule:
                start = item.get("start", 0)
                end = item.get("end", 24)
                if start <= hour < end:
                    act = str(item.get("activity") or "").strip()
                    if act:
                        return act

        # 回退到保底确定性情景池
        return self._fallback_bot_activity(hour, lt.tm_wday)

    def get_today_schedule(self) -> Dict[str, Any]:
        """获取今日的完整日程规划"""
        today_str = time.strftime("%Y-%m-%d", time.localtime())
        if self._daily_life.get("date") == today_str:
            return dict(self._daily_life)
        return {
            "date": today_str,
            "day_theme": "日常的一天",
            "schedule": [
                {
                    "start": slot["start"],
                    "end": slot["end"],
                    "period": slot["period"],
                    "activity": self._fallback_bot_activity(slot["start"], time.localtime().tm_wday),
                }
                for slot in _SCHEDULE_SLOTS
            ],
            "is_fallback": True,
        }

    def set_custom_activity(self, text: str) -> None:
        """手动设置今日自定义状态"""
        today_str = time.strftime("%Y-%m-%d", time.localtime())
        self._daily_life["date"] = today_str
        self._daily_life["custom_override"] = text.strip()
        self._save_daily_life()

    def clear_custom_activity(self) -> None:
        self._daily_life.pop("custom_override", None)
        self._save_daily_life()

    @staticmethod
    def _fallback_bot_activity(hour: int, wday: int) -> str:
        """兜底情景池"""
        is_weekend = wday >= 5
        if 0 <= hour < 6:
            activities = [
                "缩在暖烘烘的被窝里听白噪音，眼睛快要睁不开了，正处于半梦半醒的迷糊状态",
                "开着一盏微弱的暖黄色小夜灯，抱着软绵绵的抱枕快要睡着了",
                "静静躺在床上数羊，房间里很安静，偶尔听听窗外的风声",
            ]
        elif 6 <= hour < 9:
            activities = [
                "刚揉揉眼睛爬起来，正伸着懒腰看窗外初升的晨光，手里端着一杯温热的水",
                "正在整理自己的小书桌，准备享受一份热气腾腾的早点",
                "刚洗漱完毕，呼吸着清晨清爽的空气，正准备开始新的一天",
            ]
        elif 9 <= hour < 12:
            if is_weekend:
                activities = [
                    "享受着不用早起的周末上午，边吃小点心边悠闲地刷着番剧",
                    "在房间里整理喜欢的手办和小物件，心情很轻松",
                ]
            else:
                activities = [
                    "坐在窗边认真整理笔记和桌面文件，耳机里放着轻快的纯音乐",
                    "正在专注地看一些技术和有趣的新知识，桌旁放着一杯常温茶水",
                ]
        elif 12 <= hour < 14:
            activities = [
                "刚刚吃饱午饭，正满足地靠在椅背上揉肚子，有点犯困想小憩一会儿",
                "在沙发上抱着大玩偶发呆，享受午休后的片刻宁静",
            ]
        elif 14 <= hour < 17:
            if is_weekend:
                activities = [
                    "周六日的下午茶时光，一边吃着香脆的小饼干一边逛论坛看好玩的梗",
                    "在阳台的小椅子上晒太阳看故事书，微风吹得人非常惬意",
                ]
            else:
                activities = [
                    "午后稍微感到一点点疲倦，正在桌前做眼保健操并喝着下午茶提神",
                    "正在有条不紊地忙碌着手头的事情，等待着晚霞与黄昏",
                ]
        elif 17 <= hour < 19:
            activities = [
                "趴在阳台栏杆上看天边绚烂的晚霞与落日，感受着傍晚凉爽的微风",
                "正在准备美味的晚餐，厨房里飘着热气腾腾的香味",
            ]
        elif 19 <= hour < 22:
            activities = [
                "刚吃完晚饭散步回来，正坐在地毯上看最新的动漫更新，感觉特别放松",
                "在自己的小窝里听喜欢的歌，随手涂鸦画画，心情格外宁静",
                "在整理房间并准备睡前洗漱，今天也是充实又平凡的一天呢",
            ]
        else:
            activities = [
                "换上了舒服的睡衣裹在被子里刷手机，准备酝酿睡意了",
                "靠在床头看睡前读物，打了个小小的哈欠，房间里弥漫着薰衣草的香气",
            ]
        index = (wday * 24 + hour) % len(activities)
        return activities[index]

    async def generate_daily_schedule(
        self,
        context: Any,
        provider_id: str,
        prompt_library: Any,
        target_date: str = "",
        persona_prompt: str = "",
        recent_topics: str = "",
    ) -> bool:
        """
        调用模型自主规划某一天的 24 小时生活日程
        """
        if self._is_generating_life:
            return False
        self._is_generating_life = True

        try:
            if not target_date:
                target_date = time.strftime("%Y-%m-%d", time.localtime())

            dt = datetime.strptime(target_date, "%Y-%m-%d")
            wday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            wday_str = wday_names[dt.weekday()]
            is_weekend_str = "周末公休" if dt.weekday() >= 5 else "工作日/平日"

            # 节日感知
            month_day = dt.strftime("%m-%d")
            holiday_map = {
                "01-01": "元旦节",
                "02-14": "情人节",
                "03-08": "妇女节",
                "05-01": "劳动节",
                "06-01": "儿童节",
                "10-01": "国庆节",
                "10-31": "万圣夜",
                "12-24": "平安夜",
                "12-25": "圣诞节",
            }
            special_day = holiday_map.get(month_day, "")
            day_type_desc = f"{wday_str}（{is_weekend_str}）" + (f"，今天是【{special_day}】！" if special_day else "")

            prompt_tmpl = prompt_library.get("bot_daily_schedule_prompt")
            prompt = prompt_tmpl.format(
                date=target_date,
                day_type=day_type_desc,
                persona=persona_prompt or "可爱的陪伴型机器人伙伴",
                recent_topics=recent_topics or "（近期与大家轻松闲聊，享受日常生活）",
            )

            logger.info(f"[Fiscok's][bot_life] 正在使用模型规划 {target_date} ({day_type_desc}) 的 24 小时自主生活日程...")
            provider = await get_aux_provider(context, provider_id)

            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt="你是机器人角色的生活轨迹自主规划器。只输出合法的 JSON 对象，不要附加任何其他内容。",
            )

            completion = str(getattr(resp, "completion_text", "") or "").strip()
            if not completion:
                logger.warning("[Fiscok's][bot_life] 生成日程模型返回为空，跳过")
                return False

            clean_json = re.sub(r'^```json\s*', '', completion, flags=re.MULTILINE)
            clean_json = re.sub(r'\s*```$', '', clean_json, flags=re.MULTILINE).strip()
            data = json.loads(clean_json)

            day_theme = str(data.get("day_theme") or f"{target_date} 的日常生活").strip()
            raw_schedule = data.get("schedule") or []

            # 对齐标准 8 个时段
            aligned_schedule = []
            for i, slot in enumerate(_SCHEDULE_SLOTS):
                act = ""
                if i < len(raw_schedule):
                    item = raw_schedule[i]
                    if isinstance(item, dict):
                        act = str(item.get("activity") or "").strip()
                    elif isinstance(item, str):
                        act = item.strip()
                if not act:
                    act = self._fallback_bot_activity(slot["start"], dt.weekday())

                aligned_schedule.append({
                    "start": slot["start"],
                    "end": slot["end"],
                    "period": slot["period"],
                    "activity": act,
                })

            self._daily_life = {
                "date": target_date,
                "day_theme": day_theme,
                "schedule": aligned_schedule,
                "generated_at": datetime.now().isoformat(),
            }
            self._save_daily_life()
            logger.info(
                f"[Fiscok's][bot_life] 成功生成 {target_date} 日常生活日程！今日主题：{day_theme}"
            )
            return True

        except Exception as e:
            logger.warning(f"[Fiscok's][bot_life] 自主生成生活日程失败: {e}")
            return False
        finally:
            self._is_generating_life = False

    # ---------------- 用户活跃画像与作息提示 ----------------

    def record_user_activity_hour(self, user_id: str, hour: int = -1) -> None:
        """记录用户发言时段，用于推算作息习惯"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        if hour < 0:
            hour = time.localtime().tm_hour

        habits = self._user_habits.setdefault(user_id, {"night": 0, "day": 0, "early": 0})
        if 0 <= hour < 5:
            habits["night"] = habits.get("night", 0) + 1
        elif 5 <= hour < 9:
            habits["early"] = habits.get("early", 0) + 1
        else:
            habits["day"] = habits.get("day", 0) + 1

    def get_user_habit_hint(self, user_id: str) -> str:
        """获取用户作息习惯提示"""
        user_id = str(user_id or "").strip()
        habits = self._user_habits.get(user_id)
        if not habits:
            return ""
        night = habits.get("night", 0)
        early = habits.get("early", 0)
        day = habits.get("day", 0)
        total = night + early + day
        if total < 5:
            return ""

        if night >= 3 and (night / total) >= 0.25:
            return "（该用户经常在深夜熬夜，是个典型的夜猫子）"
        if early >= 3 and (early / total) >= 0.2:
            return "（该用户作息非常规律或起得很早，是个早起型作息）"
        return "（该用户为常规白天作息）"

    def get_circadian_hint(self, user_id: str = "") -> str:
        """生成符合当前时段的生活氛围、机器人自身虚拟日常及对方作息提示"""
        lt = time.localtime()
        hour = lt.tm_hour
        wday = lt.tm_wday

        wday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        today_name = wday_names[wday]

        if hour < 6:
            period = "凌晨（深夜万籁俱寂，若开口语气应轻柔、带点困意或催促对方早点休息）"
        elif hour < 9:
            period = "清晨（新的一天开始，轻松温和的早安问候）"
        elif hour < 11:
            period = "上午（学习或工作时间，不宜过度打扰）"
        elif hour < 14:
            period = "午间（午休或午餐时间，可以聊聊午饭、日常闲暇）"
        elif hour < 18:
            period = "下午（工作日可能感到些许疲劳，或是茶歇闲暇）"
        elif hour < 22:
            period = "夜晚（一天的放松时间，适宜分享日常琐碎、好奇趣事）"
        else:
            period = "深夜（接近就寝时间，适合温柔关切或道晚安）"

        current_time_str = f"{today_name} {period} {lt.tm_hour:02d}:{lt.tm_min:02d}"
        bot_activity = self.get_bot_current_activity(hour)
        user_hint = self.get_user_habit_hint(user_id) if user_id else ""
        theme = self._daily_life.get("day_theme")
        theme_str = f"（今日生活主题：{theme}）" if theme else ""

        hint = f"当前时间节点：{current_time_str}\n你此刻的虚拟日常状态：{bot_activity} {theme_str}"
        if user_hint:
            hint += f"\n对方的作息习惯：{user_hint}"
        return hint

    # ---------------- 主动发言状态持久化 ----------------

    def get_or_create_state(
        self,
        umo: str,
        is_private: bool,
        sender_id: str = "",
        nickname: str = "",
    ) -> Dict[str, Any]:
        """获取或初始化某会话/私聊的主动发言状态"""
        state = self._states.get(umo)
        if state is None:
            state = {
                "is_private": is_private,
                "sender_id": sender_id,
                "nickname": nickname,
                "next_try": 0.0,
                "last_spoke_at": 0.0,
                "daily_date": "",
                "daily_count": 0,
                "last_messages": [],
            }
            self._states[umo] = state
            self._save()
        else:
            if is_private and sender_id:
                state["sender_id"] = sender_id
            if nickname:
                state["nickname"] = nickname
        return state

    def record_spoke(
        self,
        umo: str,
        message: str,
        next_try: float,
        max_last_messages: int = 3,
    ) -> None:
        """记录主动发言成功"""
        state = self._states.get(umo)
        if not state:
            return
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))

        if state.get("daily_date") != today:
            state["daily_date"] = today
            state["daily_count"] = 0

        state["last_spoke_at"] = now
        state["daily_count"] = state.get("daily_count", 0) + 1
        state["next_try"] = next_try

        last_msgs = state.setdefault("last_messages", [])
        last_msgs.append(message)
        if max_last_messages > 0 and len(last_msgs) > max_last_messages:
            del last_msgs[: len(last_msgs) - max_last_messages]

        self._save()
        logger.info(
            f"[ProactiveService] 会话 {umo} 今日已主动发言 {state['daily_count']} 次"
        )

    def set_next_try(self, umo: str, next_try: float) -> None:
        """更新下次重试时间"""
        state = self._states.get(umo)
        if state:
            state["next_try"] = next_try

    def all_states(self) -> Dict[str, Dict[str, Any]]:
        return self._states
