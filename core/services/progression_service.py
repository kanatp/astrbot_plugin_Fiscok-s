"""
话题自动行进服务 (TopicProgressionService)

实现功能：
1. 自主连续任务推进：模型自行规划下一步并在末尾输出 [CONTINUE: 下一步]，引擎自动延迟发起下一轮请求，连续发送；
2. 用户插话合并与倒计时重置：在等待发送下一阶段期间，若用户发言，合并用户提问并重置等待时间，在下一轮中优先解答再推进；
3. 随时终止保障：检测到用户说“停止/别讲了/打住”等意图时安全退出；
4. 严格规范的日志追踪：
   - ［log］开启了持续进行任务
   - ［log］在发送下一任务前等待中
   - ［log］收到用户信息，重置等待时间
   - ［log］下一请求已发送
   - ［log］在发送下一任务前等待中
   - ［log］模型判断任务完成（注：用户终止也输出这个）
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from ..api.provider_utils import get_aux_provider

_PROGRESSION_TAG_RE = re.compile(r'\[(CONTINUE\s*:[^\]]*|DONE)\]', re.IGNORECASE)
_DEFAULT_STOP_WORDS = ("停止", "停下", "别讲了", "闭嘴", "打住", "结束任务", "不用说了", "不用讲了", "退下")


class ProgressionSession:
    def __init__(
        self,
        umo: str,
        user_id: str,
        nickname: str,
        task_goal: str,
        next_step: str,
        max_rounds: int = 12,
        interval_seconds: float = 5.0,
        reset_seconds: float = 3.5,
        llm_provider_id: str = "",
    ):
        self.umo = umo
        self.user_id = user_id
        self.nickname = nickname
        self.task_goal = task_goal
        self.next_step = next_step
        self.round = 1
        self.max_rounds = max(1, max_rounds)
        self.interval_seconds = max(1.0, interval_seconds)
        self.reset_seconds = max(1.0, reset_seconds)
        self.llm_provider_id = llm_provider_id
        self.pending_user_messages: List[str] = []
        self.timer_task: Optional[asyncio.Task] = None
        self.is_waiting: bool = False
        self.is_active: bool = True
        self.started_at: float = time.time()
        self.last_active_at: float = time.time()


class TopicProgressionService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._sessions: Dict[str, ProgressionSession] = {}

    def is_active(self, umo: str) -> bool:
        """检查当前会话是否有正在进行的持续任务"""
        session = self._sessions.get(umo)
        return bool(session and session.is_active)

    def get_session(self, umo: str) -> Optional[ProgressionSession]:
        return self._sessions.get(umo)

    def get_all_active_sessions(self) -> List[Dict[str, Any]]:
        """获取所有正在运行的任务状态信息，供 WebUI 或管理指令查询"""
        result = []
        for umo, s in self._sessions.items():
            if s.is_active:
                result.append({
                    "umo": umo,
                    "user_id": s.user_id,
                    "nickname": s.nickname,
                    "task_goal": s.task_goal,
                    "next_step": s.next_step,
                    "round": s.round,
                    "max_rounds": s.max_rounds,
                    "is_waiting": s.is_waiting,
                    "pending_count": len(s.pending_user_messages),
                    "started_at": s.started_at,
                })
        return result

    def stop_session(self, umo: str, reason: str = "") -> bool:
        """终止并清理指定会话的自动行进任务"""
        session = self._sessions.pop(umo, None)
        if not session:
            return False

        session.is_active = False
        session.is_waiting = False
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()

        log_reason = f"（{reason}）" if reason else ""
        logger.info(f"[Fiscok's][progression] ［log］模型判断任务完成{log_reason}")
        return True

    def check_and_strip_tags(self, text: str) -> tuple[str, Optional[str], bool]:
        """
        从文本中检查并裁剪连续控制标记。
        :return: (clean_text, next_step, is_done)
        """
        if not text:
            return text, None, False

        match = _PROGRESSION_TAG_RE.search(text)
        if not match:
            return text, None, False

        raw_tag = match.group(1).strip()
        clean_text = _PROGRESSION_TAG_RE.sub('', text).strip()
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)

        if raw_tag.upper() == "DONE":
            return clean_text, None, True

        if raw_tag.upper().startswith("CONTINUE"):
            parts = raw_tag.split(":", 1)
            next_step = parts[1].strip() if len(parts) > 1 else "接续推进下一阶段"
            return clean_text, next_step, False

        return clean_text, None, False

    def on_model_reply(
        self,
        umo: str,
        user_id: str,
        nickname: str,
        raw_text: str,
        config: Dict[str, Any],
    ) -> str:
        """
        在模型回复完成时调用，检测是否包含继续或结束标记，并启动/推进任务。
        :return: 裁剪掉标记后的干净文本供发送
        """
        clean_text, next_step, is_done = self.check_and_strip_tags(raw_text)

        if not config.get("available", True):
            return clean_text

        if is_done:
            if self.is_active(umo):
                self.stop_session(umo, "模型回复 [DONE]")
            return clean_text

        if next_step:
            # 检查群聊权限
            is_group = ":group:" in umo or ":group_message:" in umo
            if is_group and not config.get("allow_group", True):
                return clean_text

            max_rounds = int(config.get("max_rounds", 12) or 12)
            interval = float(config.get("interval_seconds", 5.0) or 5.0)
            reset_sec = float(config.get("reset_seconds_on_user_input", 3.5) or 3.5)
            llm_provider = str(config.get("llm_provider_id", "") or "")

            session = self._sessions.get(umo)
            if not session or not session.is_active:
                task_goal = clean_text[:80] + ("…" if len(clean_text) > 80 else "")
                session = ProgressionSession(
                    umo=umo,
                    user_id=user_id,
                    nickname=nickname,
                    task_goal=task_goal,
                    next_step=next_step,
                    max_rounds=max_rounds,
                    interval_seconds=interval,
                    reset_seconds=reset_sec,
                    llm_provider_id=llm_provider,
                )
                self._sessions[umo] = session
                logger.info(f"[Fiscok's][progression] ［log］开启了持续进行任务（会话: {umo}）")
            else:
                session.next_step = next_step
                session.round += 1

            self._schedule_next_step(session)

        return clean_text

    async def handle_user_intercept(self, event: Any, config: Dict[str, Any]) -> bool:
        """
        在用户消息到达时进行拦截处理（高优先级钩子调用）。
        :return: True 表示当前消息继续正常传递；False 表示消息已被行进引擎捕获/拦截，调用方应停止流转。
        """
        umo = getattr(event, "unified_msg_origin", "")
        session = self._sessions.get(umo)
        if not session or not session.is_active:
            return True

        # 若不在等待倒计时窗口中，则不作为插话拦截
        if not session.is_waiting:
            return True

        message = (getattr(event, "message_str", "") or "").strip()
        if not message:
            return True

        # 检查是否命中终止关键词
        stop_keywords_raw = str(config.get("stop_keywords", "") or "")
        stop_words = [w.strip() for w in re.split(r'[,，\s]+', stop_keywords_raw) if w.strip()]
        if not stop_words:
            stop_words = list(_DEFAULT_STOP_WORDS)

        if any(w in message for w in stop_words):
            self.stop_session(umo, "用户输入终止指令")
            if hasattr(event, "stop_event"):
                event.stop_event()
            # 发送收尾反馈
            try:
                await self.plugin.context.send_message(
                    umo, MessageChain(chain=[Plain("好的，持续任务已为你停止。")])
                )
            except Exception:
                pass
            return False

        # 捕获用户插话，重置等待计时器
        session.pending_user_messages.append(message)
        logger.info(f"[Fiscok's][progression] ［log］收到用户信息，重置等待时间（内容: {message[:40]}）")

        # 取消之前的定时器，重新以 reset_seconds 倒计时
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()

        session.timer_task = asyncio.create_task(
            self._wait_and_proceed(session, delay=session.reset_seconds)
        )

        if hasattr(event, "stop_event"):
            event.stop_event()
        return False

    def _schedule_next_step(self, session: ProgressionSession) -> None:
        """开启下一轮任务等待"""
        session.is_waiting = True
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()

        logger.info(f"[Fiscok's][progression] ［log］在发送下一任务前等待中（预计等待 {session.interval_seconds:.1f}s）")
        session.timer_task = asyncio.create_task(
            self._wait_and_proceed(session, delay=session.interval_seconds)
        )

    async def _wait_and_proceed(self, session: ProgressionSession, delay: float) -> None:
        """延迟等待并执行下一轮推进"""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        session.is_waiting = False
        if not session.is_active:
            return

        await self._execute_next_step(session)

    async def _execute_next_step(self, session: ProgressionSession) -> None:
        """组装上下文并向 LLM 发起推进请求"""
        umo = session.umo
        if session.round > session.max_rounds:
            self.stop_session(umo, f"已达最大连续推进轮数上限 ({session.max_rounds})")
            return

        logger.info(f"[Fiscok's][progression] ［log］下一请求已发送（第 {session.round} 轮）")

        # 组装推进输入提示词
        user_interruption = ""
        if session.pending_user_messages:
            merged_user_msg = "；".join(session.pending_user_messages)
            session.pending_user_messages.clear()
            user_interruption = (
                f"\n【用户在中途提出的反馈/疑问】：{merged_user_msg}\n"
                f"（请先自然且切题地回应并解答用户的疑问，然后再顺畅地接续后续任务内容）\n"
            )

        step_prompt = (
            f"请自主推进当前正在进行的任务：\n"
            f"【任务总体目标】：{session.task_goal}\n"
            f"【当前阶段推进要求】：{session.next_step}\n"
            f"{user_interruption}"
            f"请直接给出这一阶段的内容输出。如果任务已全部圆满结束，请在末尾附带 [DONE]；"
            f"如果任务仍需继续进行下一步，请在末尾附带 [CONTINUE: 下一步具体要做什么]。"
        )

        try:
            context = self.plugin.context
            provider = await get_aux_provider(context, session.llm_provider_id)

            # 获取近期对话上下文
            contexts = await self.plugin.conversation_contexts(umo, limit=8)

            response = await provider.text_chat(
                prompt=step_prompt,
                contexts=contexts,
                system_prompt=getattr(provider, "default_system_prompt", "") or "",
            )
            raw_reply = str(getattr(response, "completion_text", "") or "").strip()
            if not raw_reply:
                self.stop_session(umo, "模型返回为空")
                return

            # 解析控制标记
            clean_text, next_step, is_done = self.check_and_strip_tags(raw_reply)

            # 发送给用户（支持分段与拟真打字延迟）
            await self._send_progression_reply(umo, clean_text)

            # 写回 AstrBot 会话历史
            await self.plugin._append_assistant_history(umo, clean_text)

            if is_done or not next_step:
                self.stop_session(umo, "模型任务目标达成")
            else:
                session.round += 1
                session.next_step = next_step
                self._schedule_next_step(session)

        except Exception as e:
            logger.warning(f"[Fiscok's][progression] 执行自主推进请求异常: {e}")
            self.stop_session(umo, f"异常中断: {e}")

    async def _send_progression_reply(self, umo: str, text: str) -> None:
        """发送推进结果消息，配合拟真延迟与分段"""
        if not text:
            return

        seg_cfg = self.plugin.config.get("segmented_parser_config", {})
        if seg_cfg.get("available", False):
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            base_interval = float(seg_cfg.get("send_interval", 0.5) or 0.5)
            dynamic_typing = seg_cfg.get("dynamic_typing", True)

            for i, line in enumerate(lines):
                if dynamic_typing and hasattr(self.plugin, "typing_service"):
                    delay = self.plugin.typing_service.calculate_delay(line, base=base_interval)
                else:
                    delay = base_interval

                if i > 0 and delay > 0:
                    await asyncio.sleep(delay)

                await self.plugin.context.send_message(
                    umo, MessageChain(chain=[Plain(line)])
                )
        else:
            await self.plugin.context.send_message(
                umo, MessageChain(chain=[Plain(text)])
            )
