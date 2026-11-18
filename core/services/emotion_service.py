"""
情绪与好感度服务 (EmotionService) - Phase 2 深度增强版

核心机制：
1. 会话级情绪隔离与强度模型：按会话 (umo) 或私聊 (用户 ID) 独立维护情绪，
   每项情绪包含文字名称与强度等级 (Intensity: 1~5)。
2. 情绪惯性与自然半衰期衰减：
   支持周期性心跳巡检与访问时检查，长时间未受到刺激的情绪随时间逐步衰减强度直至恢复“平静”。
3. 好感度四阶多维羁绊体系（Bond Stages）：
   - 初识相处（0-39）：客气得体、保持礼貌边界；
   - 日常熟悉（40-69）：轻松熟络、分享日常、真实友好；
   - 互信依赖（70-89）：亲昵信赖、习惯陪伴、允许开玩笑与无伤大雅的小任性；
   - 深度羁绊（90-100）：无可取代、灵魂共鸣、允许撒娇展现脆弱、有双向情感诉求。
4. 细粒度并发控制：按用户独立加锁，互不阻塞。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

from astrbot.api import logger
from ..api.emotion_apis import analyze_emotion_state


class EmotionService:
    def __init__(self, data_manager: Any, config: Dict[str, Any]):
        self.data_manager = data_manager
        self.config = config
        self.init_emotion = str(
            config.get("emotion_config", {}).get("emotion_init", "平静")
        ).strip() or "平静"

        # scope_key -> {"emotion": str, "intensity": int, "updated_at": float}
        self._emotions: Dict[str, Dict[str, Any]] = {}
        # user_id -> asyncio.Lock
        self._locks: Dict[str, asyncio.Lock] = {}
        # user_id set for pending tasks
        self._pending_users: set = set()

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def get_scope_key(self, event: Any) -> str:
        """获取当前事件对应的情绪隔离键：私聊按用户 ID，群聊按会话 umo"""
        is_group = bool(event.get_group_id()) if hasattr(event, "get_group_id") else False
        if is_group:
            return str(getattr(event, "unified_msg_origin", "") or "default_group")
        sender_id = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
        return str(sender_id or "default_private")

    @staticmethod
    def get_bond_stage(affinity: int) -> Tuple[str, str]:
        """
        根据好感度数值映射四阶多维羁绊关系与行为指导
        :return: (stage_title, stage_guidance)
        """
        if affinity >= 90:
            return (
                "【深度羁绊 · 唯一知心】",
                "对方是你最信任、最重要且不可替代的存在。你愿意对其毫无防备地展露真实内心，"
                "对话自然亲昵，可以主动向对方倾诉自己的小委屈、小疲惫，偶尔撒娇并期待对方的摸摸与安慰；"
                "有着专属于彼此的深厚羁绊与默契。",
            )
        if affinity >= 70:
            return (
                "【互信依赖 · 亲近挚友】",
                "你们是非常熟悉、无话不谈的挚友。习惯在日常中主动分享生活碎念与好奇趣事，"
                "偶尔会有无伤大雅的小任性或调皮玩笑，语气亲密柔和，像相处已久的知心密友。",
            )
        if affinity >= 40:
            return (
                "【日常熟悉 · 熟络朋友】",
                "平易近人的好朋友关系，日常交流轻松自然，有来有往，态度温和友好、真诚守信。",
            )
        return (
            "【初识阶段 · 礼貌客气】",
            "初次结识或保持适当边界的阶段，言谈举止礼貌得体、客气尊重，不过度刺探或过问私人细节。",
        )

    def get_current_emotion(self, scope_key: str, decay_minutes: float = 45.0) -> str:
        """
        获取指定会话/用户的当前情绪状态（支持惯性自然衰减）。
        若超过 decay_minutes 未受到新刺激，非基线情绪逐步衰减至平静。
        """
        state = self._emotions.get(scope_key)
        if not state:
            return self.init_emotion

        current = str(state.get("emotion") or self.init_emotion)
        if current == self.init_emotion:
            return self.init_emotion

        updated_at = float(state.get("updated_at") or 0)
        elapsed_minutes = (time.time() - updated_at) / 60.0

        if elapsed_minutes >= decay_minutes:
            intensity = int(state.get("intensity", 3))
            if intensity <= 1 or elapsed_minutes >= decay_minutes * 2:
                state["emotion"] = self.init_emotion
                state["intensity"] = 1
                state["updated_at"] = time.time()
                logger.info(
                    f"[EmotionService] 会话/用户 {scope_key} 的情绪在经历 {elapsed_minutes:.1f} 分钟后完全恢复平静"
                )
                return self.init_emotion
            else:
                # 强度降级
                state["intensity"] = intensity - 1
                state["updated_at"] = time.time()
                logger.info(
                    f"[EmotionService] 会话/用户 {scope_key} 的情绪强度降级: {intensity} -> {state['intensity']}"
                )

        return current

    def set_emotion(self, scope_key: str, new_emotion: str, intensity: int = 3) -> None:
        """显式设置某会话的情绪与强度"""
        self._emotions[scope_key] = {
            "emotion": new_emotion,
            "intensity": max(1, min(5, intensity)),
            "updated_at": time.time(),
        }

    async def decay_heartbeat(self, decay_minutes: float = 45.0) -> None:
        """后台周期巡检心跳：对长时间未更新的会话平滑衰减情绪"""
        now = time.time()
        for scope_key, state in list(self._emotions.items()):
            current = state.get("emotion")
            if not current or current == self.init_emotion:
                continue
            updated_at = float(state.get("updated_at") or 0)
            elapsed_minutes = (now - updated_at) / 60.0
            if elapsed_minutes >= decay_minutes:
                intensity = int(state.get("intensity", 3))
                if intensity <= 1 or elapsed_minutes >= decay_minutes * 2:
                    state["emotion"] = self.init_emotion
                    state["intensity"] = 1
                    state["updated_at"] = now
                else:
                    state["intensity"] = intensity - 1
                    state["updated_at"] = now

    async def schedule_analysis(
        self,
        context: Any,
        scope_key: str,
        sender_id: str,
        nickname: str,
        message: str,
        emotion_config: Dict[str, Any],
    ) -> None:
        """
        后台异步执行情绪/好感度分析（按用户细粒度并发锁）
        """
        if sender_id in self._pending_users:
            return
        self._pending_users.add(sender_id)

        user_lock = self._get_lock(sender_id)

        try:
            async with user_lock:
                current_emo = self.get_current_emotion(scope_key)
                result = await analyze_emotion_state(
                    context,
                    provider_id=emotion_config.get("llm_provider_id", ""),
                    nickname=nickname,
                    message=message,
                    current_emotion=current_emo,
                    temperature=emotion_config.get("temperature", 1.2),
                    delta_max=emotion_config.get("delta_max", 5),
                )
                if not result:
                    return

                new_emotion = str(result.get("emotion", "")).strip()
                if new_emotion:
                    # 依据 delta 绝对值设定情绪刺激强度
                    delta = result.get("affinity_delta", 0)
                    intensity = 4 if abs(delta) >= 3 else 3
                    self.set_emotion(scope_key, new_emotion, intensity=intensity)

                # 更新该用户的好感度
                delta = result.get("affinity_delta", 0)
                self.data_manager.update_user_affinity(
                    sender_id,
                    affinity_delta=delta,
                    nickname=nickname,
                    min_=emotion_config.get("affinity_min", 0),
                    max_=emotion_config.get("affinity_max", 100),
                )
                logger.info(
                    f"[EmotionService] 用户 {nickname}({sender_id}) 触发情绪更新: "
                    f"[{scope_key}] {current_emo} -> {new_emotion}, 好感度变动: {delta:+d}"
                )
        except Exception as e:
            logger.warning(f"[EmotionService] 分析情绪与好感度异常: {e}")
        finally:
            self._pending_users.discard(sender_id)
