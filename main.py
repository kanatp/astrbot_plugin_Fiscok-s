from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.message_components import Node, Plain, Image, Nodes, Reply, Forward

from .core.api.bili_apis import get_bvid
from .core.api.storage_apis import DataManager
from .core.api.meme_apis import generate_meme_description
from .core.api.emotion_apis import analyze_emotion_state
from .core.api.provider_utils import close_aux_providers
from .core.api.topic_memory_apis import (
    get_text_embedding,
    decide_topic_memory,
    retrieve_topics,
    resolve_embedding_provider,
)
from .core.api.topic_tracking_apis import decide_topic_continuation
from .core.api.proactive_chat_apis import generate_proactive_message
from .core.api.persona_evolution_apis import evolve_persona
from astrbot.core.message.message_event_result import MessageChain
from .core.prompts import (
    format_meme_placeholder_injection,
    format_emotion_state_injection,
    format_persona_overlay_injection,
    format_topic_memory_injection,
    TOPIC_TRACKING_REPLY_INJECTION,
)
from .core.net.twitter_fetch import fetch_twitter_data, check_availability
from .core.net.instagram_fetch import create_loader, fetch_instagram_posts, fetch_instagram_stories, check_instagram_access

import subprocess
import asyncio
import random
import time
import aiohttp
import aiofiles
import json
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing import List, Dict, Any
import re

@register("Fiscok-s Plugins", "Fiscok", "Fiscok自用插件", "2.2")
class Core(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.running = True

        self.config = config
        self.plugin_data_path = get_astrbot_data_path() + "/plugin_data/" + self.name
        self.data_manager = DataManager(self.plugin_data_path, config)

        # 模型自身的全局情绪状态（运行时变量，文字描述，不持久化）
        self.current_emotion = str(config.get('emotion_config', {}).get('emotion_init', '平静'))

        # 后台任务：辅助调用（情绪分析/表情包学习）异步执行，避免阻塞主对话
        self._background_tasks: set = set()
        self._emotion_lock = asyncio.Lock()
        self._emotion_pending: set = set()
        self._topic_pending: set = set()

        # 群聊话题跟踪会话（umo -> 会话状态），仅内存驻留
        self._topic_sessions: Dict[str, Dict] = {}

        # 主动发起聊天：群聊最近消息缓冲与各群发言状态（仅内存驻留）
        self._group_recent: Dict[str, List[str]] = {}
        self._group_last_activity: Dict[str, float] = {}
        self._proactive_states: Dict[str, Dict] = {}

        # 人设演化：插件侧可演化、可回滚的“自我设定”，与原生人设叠加注入
        self._persona_overlay = str(self.data_manager.get_evolved_persona().get('persona', '') or '')
        self._persona_recent: Dict[str, List[str]] = {}
        self._persona_reply_count = 0
        self._persona_evolve_pending = False

        self.rssHub_base_url = self.config.get('twitter_subscription_config', {}).get("rssHub_url", "")
        self.rssHub_port = self.config.get('twitter_subscription_config', {}).get("rssHub_port", 1200)
        self.rssHub_full_url = f"{self.rssHub_base_url}:{self.rssHub_port}" if self.rssHub_base_url else ""
        if not self.rssHub_base_url:
            logger.warning(f"[Fiscok's][twitter_push]未配置 RSSHub 基础 URL，推特订阅功能将无法使用，请在配置中添加 rsshub_base_url")

        # 添加缓存轮询更新任务
        asyncio.create_task(self.twitter_cache_update())

        # 添加定时推送推特内容任务
        self.timer = AsyncIOScheduler()
        time_list = self.config.get('twitter_subscription_config', {}).get("twitter_push_time", [])
        for time_str in time_list:
            self.timer.add_job(
                self.twitter_scheduled_push,
                'cron',
                hour=int(time_str.split(":")[0]),
                minute=int(time_str.split(":")[1])
            )

        # --- 加载统一 cookie 文件 ---
        self.cookies_path = Path(self.plugin_data_path) / "cookies.json"
        self.cookies = self._load_cookies()

        # --- Instagram 订阅初始化 ---
        self.ins_loader = None
        ins_config = self.config.get('instagram_subscription_config', {})
        if ins_config.get('instagram_subscription_available', False):
            ins_cookies = self.cookies.get('instagram', {})
            self.ins_loader = create_loader(ins_cookies)
            if self.ins_loader:
                asyncio.create_task(self.instagram_cache_update())
                for time_str in ins_config.get('instagram_push_time', []):
                    self.timer.add_job(
                        self.instagram_scheduled_push,
                        'cron',
                        hour=int(time_str.split(":")[0]),
                        minute=int(time_str.split(":")[1])
                    )
                logger.info("[Fiscok's][instagram] Instagram 订阅功能已初始化")
            else:
                logger.warning("[Fiscok's][instagram] Instagram cookies 无效或未配置，订阅功能未启用")

        # 启动定时任务
        self.timer.start()

        # 主动发起聊天：周期性检查冷场群聊
        self._spawn_background(self._proactive_loop())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def meme_learn_on_message(self, event: AstrMessageEvent):
        """
        在每次收到消息时触发表情包偷取判定（后台异步执行，不阻塞主对话）
        """
        meme_config = self.config.get('meme_config', {})
        if not meme_config.get('meme_available', False):
            return

        # 忽略引用和转发消息
        if event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, (Reply, Forward)):
                    return

        # 偷取概率随表情包数量衰减
        learn_max = meme_config.get('emoji_learn_max', 0.3)
        learn_min = meme_config.get('emoji_learn_min', 0.02)
        current_count = self.data_manager.get_meme_count()
        max_cache = meme_config.get('meme_cache_size', 200)
        learn_probability = learn_max - (learn_max - learn_min) * min(current_count / max_cache, 1.0)
        learn_probability = max(learn_probability, learn_min)

        if random.random() >= learn_probability:
            return

        # 同步提取图片地址与来源后再交给后台任务，避免 event 被回收/复用后读到错误数据
        image_urls = self._extract_emoji_urls(event)
        if not image_urls:
            return
        source = f"group_{event.get_group_id()}" if event.get_group_id() else "private"
        self._spawn_background(self._learn_meme_from_message(image_urls, source, meme_config))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def ensure_user_affinity_on_message(self, event: AstrMessageEvent):
        """
        在每次收到消息时确保该用户的好感度记录已初始化（新用户从初始值开始，默认 50）
        """
        emotion_config = self.config.get('emotion_config', {})
        if not emotion_config.get('available', False):
            return

        sender_id = event.get_sender_id()
        if not sender_id or sender_id == event.get_self_id():
            return

        try:
            self.data_manager.get_user_affinity(sender_id, event.get_sender_name())
        except Exception as e:
            logger.warning(f"[Fiscok's][emotion] 初始化用户好感度失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def topic_learn_on_message(self, event: AstrMessageEvent):
        """
        在每次收到消息时触发话题记忆判定（后台异步执行，不阻塞主对话）
        """
        topic_config = self.config.get('topic_memory_config', {})
        if not topic_config.get('available', False):
            return

        # 忽略引用和转发消息
        if event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, (Reply, Forward)):
                    return

        # 概率节流，控制辅助 LLM 成本
        if random.random() >= topic_config.get('analyze_probability', 0.2):
            return

        message = event.message_str
        if not message:
            return

        # 同步取出必要信息后再交给后台任务，避免 event 被回收/复用后读到错误数据
        sender_id = event.get_sender_id()
        if not sender_id or sender_id == event.get_self_id():
            return
        self._schedule_topic_learning(sender_id, event.get_sender_name(), message, topic_config)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def topic_tracking_on_message(self, event: AstrMessageEvent):
        """
        群聊话题跟踪：机器人发言后开启话题，在话题存续期间判定新消息是否相关，
        由模型决定是否续聊以及何时结束参与
        """
        track_config = self.config.get('topic_tracking_config', {})
        if not track_config.get('available', False):
            return

        # 仅处理群聊，且只跟踪他人消息（忽略机器人自己发出的消息）
        if not event.get_group_id():
            return
        sender_id = event.get_sender_id()
        if not sender_id or sender_id == event.get_self_id():
            return

        umo = event.unified_msg_origin
        session = self._topic_sessions.get(umo)
        if session is None:
            return

        message = (event.message_str or '').strip()
        # 忽略空消息与斜杠指令
        if not message or message.startswith(('/', '／')):
            return

        nickname = event.get_sender_name() or '群友'
        ttl = float(track_config.get('ttl_seconds', 90) or 0)

        # 空闲超时：长时间无人继续该话题则结束
        if ttl > 0 and time.time() - session.get('last_active', 0) > ttl:
            self._end_topic_session(umo, '空闲超时')
            return

        self._append_recent(session, nickname, message, track_config)

        # 被 @/唤醒：本体将直接回复，此处只记录并保活
        if event.is_at_or_wake_command:
            session['last_active'] = time.time()
            return

        max_rounds = int(track_config.get('max_rounds', 8) or 0)
        if max_rounds > 0 and session.get('auto_rounds', 0) >= max_rounds:
            self._end_topic_session(umo, '达到最大续聊轮数')
            return

        # 同一话题串行判定，避免多条消息并发触发重复回复
        async with session['lock']:
            # 等待锁期间话题可能已被结束或重建
            if self._topic_sessions.get(umo) is not session:
                return
            if ttl > 0 and time.time() - session.get('last_active', 0) > ttl:
                self._end_topic_session(umo, '空闲超时')
                return
            decision = await decide_topic_continuation(
                self.context,
                provider_id=track_config.get('llm_provider_id', ''),
                nickname=nickname,
                message=message,
                topic=session.get('topic', ''),
                # 最后一条是当前消息本身，已单独作为「新消息」传入
                recent=list(session.get('recent', []))[:-1],
            )

        # 判定失败时不发言，避免误触发
        if not decision:
            logger.info(f"[Fiscok's][track] 判定无结果，跳过本次发言：{nickname}：{message[:40]}")
            return

        logger.info(
            f"[Fiscok's][track] 判定结果 respond={decision.get('respond')} "
            f"end={decision.get('end')} | {nickname}：{message[:40]}"
        )

        if decision.get('end'):
            self._end_topic_session(umo, '模型判定话题结束')
            return

        if not decision.get('respond'):
            session['idle_rounds'] = session.get('idle_rounds', 0) + 1
            max_idle = int(track_config.get('max_idle_rounds', 3) or 0)
            logger.info(
                f"[Fiscok's][track] 不接话，连续无关 {session['idle_rounds']}/{max_idle} 条"
            )
            if max_idle > 0 and session['idle_rounds'] >= max_idle:
                self._end_topic_session(umo, '连续无关消息')
            return

        session['auto_rounds'] = session.get('auto_rounds', 0) + 1
        session['idle_rounds'] = 0
        session['last_active'] = time.time()

        conversation = await self._get_or_create_conversation(event)
        logger.info(
            f"[Fiscok's][track] 续聊话题（第 {session['auto_rounds']} 轮，"
            f"话题：{session.get('topic', '')[:40]}）：{message[:40]}"
        )
        yield event.request_llm(
            prompt=message,
            conversation=conversation,
            system_prompt=TOPIC_TRACKING_REPLY_INJECTION,
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def proactive_buffer_on_message(self, event: AstrMessageEvent):
        """
        主动发起聊天：记录群聊最近消息与活跃时间，供判定“冷场”与生成话题参考
        """
        proactive_config = self.config.get('proactive_chat_config', {})
        is_private = not event.get_group_id()
        if is_private:
            if not proactive_config.get('private_available', False):
                return
        elif not proactive_config.get('available', False):
            return

        sender_id = event.get_sender_id()
        if not sender_id or sender_id == event.get_self_id():
            return
        # 私聊主动仅对白名单用户生效（留空表示不启用）
        if is_private and not self._is_user_allowed(sender_id, proactive_config):
            return

        message = (event.message_str or '').strip()
        if not message or message.startswith(('/', '／')):
            return

        umo = event.unified_msg_origin
        self._group_last_activity[umo] = time.time()
        self._append_group_line(
            umo, event.get_sender_name() or '群友', message, proactive_config
        )

    @filter.on_llm_request()
    async def on_llm_request_topic_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 请求阶段用当前消息做词向量检索，把相关话题记忆作为补充信息注入 system_prompt
        """
        topic_config = self.config.get('topic_memory_config', {})
        if not topic_config.get('available', False):
            return

        query = event.message_str
        if not query:
            return

        provider_id = topic_config.get('embedding_provider_id', '')
        try:
            # 未配置 Embedding Provider 时该功能静默禁用
            if resolve_embedding_provider(self.context, provider_id) is None:
                return

            query_vector = await get_text_embedding(self.context, provider_id, query)
            if not query_vector:
                return

            topics = self.data_manager.get_all_topics()
            if not topics:
                return

            matched = retrieve_topics(
                topics,
                query_vector,
                top_k=topic_config.get('retrieve_top_k', 3),
                min_similarity=topic_config.get('min_similarity', 0.5),
            )
            if not matched:
                return

            # 截断单条话题内容，控制注入体积
            max_chars = int(topic_config.get('max_content_chars', 200) or 0)
            if max_chars > 0:
                for topic in matched:
                    content = str(topic.get('content', ''))
                    if len(content) > max_chars:
                        topic['content'] = content[:max_chars] + '…'

            injection = format_topic_memory_injection(matched)
            if injection:
                req.system_prompt = f"{req.system_prompt or ''}\n{injection}"
                logger.info(
                    f"[Fiscok's][topic] 已注入 {len(matched)} 条相关话题记忆: "
                    f"{[t.get('keyword') for t in matched]}"
                )
        except Exception as e:
            logger.warning(f"[Fiscok's][topic] 检索/注入话题记忆失败，不影响主请求: {e}")

    @filter.on_llm_request()
    async def on_llm_request_context_trim_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        上下文精简：由插件接管请求上下文裁剪，降低随历史累积而增长的 token 开销。
        仅在配置开启时生效；默认不裁剪，保持原行为。
        """
        cfg = self.config.get('context_optimize_config', {})
        if not cfg.get('available', False):
            return

        try:
            keep_recent = int(cfg.get('keep_recent_messages', 10) or 0)
            max_chars = int(cfg.get('max_context_chars', 0) or 0)
            contexts = getattr(req, 'contexts', None)
            if not contexts:
                return

            trimmed = self._trim_contexts(contexts, keep_recent, max_chars)
            if trimmed != contexts:
                logger.info(f"[Fiscok's][context] 上下文精简：{len(contexts)} -> {len(trimmed)} 条")
                req.contexts = trimmed
        except Exception as e:
            logger.warning(f"[Fiscok's][context] 上下文精简失败，不影响主请求: {e}")

    @staticmethod
    def _trim_contexts(contexts: List[Dict], keep_recent: int, max_chars: int) -> List[Dict]:
        """
        裁剪上下文：先按最近条数截断，再按字符上限从尾部向前保留。
        至少保留最新一条；结果首条若为 assistant 则丢弃，避免角色序列异常。
        """
        if keep_recent > 0:
            contexts = contexts[-keep_recent:]

        if max_chars > 0:
            kept = []
            total = 0
            for msg in reversed(contexts):
                content = msg.get('content') if isinstance(msg, dict) else None
                if not isinstance(content, str):
                    content = str(content or '')
                if kept and total + len(content) > max_chars:
                    break
                kept.append(msg)
                total += len(content)
            contexts = list(reversed(kept))

        while contexts and isinstance(contexts[0], dict) and contexts[0].get('role') == 'assistant':
            contexts = contexts[1:]
        return contexts

    @filter.on_llm_request()
    async def on_llm_request_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在获取 LLM 回复之前拦截请求：概率注入占位符说明，引导 LLM 生成表情包占位符
        """
        meme_config = self.config.get('meme_config', {})
        if not meme_config.get('meme_available', False):
            return

        # --- 占位符注入流程 ---
        attach_probability = meme_config.get('emoji_attach_positive', 0.7)
        if random.random() < attach_probability:
            placeholder_tag = meme_config.get('placeholder_tag', 'meme')
            req.system_prompt += format_meme_placeholder_injection(placeholder_tag)

    @filter.on_llm_request()
    async def on_llm_request_emotion_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 请求阶段注入当前已知的情绪/好感度状态（不阻塞主请求）；
        情绪/好感度分析改为后台异步执行，更新结果供下一轮请求使用
        """
        emotion_config = self.config.get('emotion_config', {})
        if not emotion_config.get('available', False):
            return

        sender_id = event.get_sender_id()
        if not sender_id or sender_id == event.get_self_id():
            return

        try:
            nickname = event.get_sender_name()
            # 立即用当前已知状态注入，避免等待情绪分析
            affinity = self.data_manager.get_user_affinity(sender_id, nickname)
            injection = format_emotion_state_injection(
                nickname,
                self.current_emotion,
                affinity,
            )
            req.system_prompt = f"{req.system_prompt or ''}\n{injection}"

            # 后台异步分析，更新全局情绪与该用户好感度（供后续请求使用）
            self._schedule_emotion_analysis(sender_id, nickname, event.message_str, emotion_config)
        except Exception as e:
            logger.warning(f"[Fiscok's][emotion] 注入情绪状态失败，不影响主请求: {e}")

    @filter.on_llm_request()
    async def on_llm_request_persona_hook(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        人设演化：注入当前“自我设定”，并记录对话用于后续演化参考
        """
        persona_config = self.config.get('persona_evolution_config', {})
        if not persona_config.get('available', False):
            return

        try:
            injection = format_persona_overlay_injection(self._persona_overlay)
            if injection:
                req.system_prompt = f"{req.system_prompt or ''}\n{injection}"

            nickname = event.get_sender_name() or '对方'
            self._append_persona_line(
                event.unified_msg_origin, nickname, event.message_str, persona_config
            )
        except Exception as e:
            logger.warning(f"[Fiscok's][persona] 注入自我设定失败，不影响主请求: {e}")

    @filter.on_llm_response()
    async def on_llm_response_persona_hook(self, event: AstrMessageEvent, resp):
        """
        人设演化：记录自己的发言，按配置的回复条数周期触发一次后台演化
        """
        persona_config = self.config.get('persona_evolution_config', {})
        if not persona_config.get('available', False):
            return

        try:
            reply = (getattr(resp, 'completion_text', '') or '').strip()
            if not reply:
                return

            umo = event.unified_msg_origin
            self._append_persona_line(umo, '我', reply, persona_config)

            every = int(persona_config.get('evolve_every_messages', 10) or 0)
            if every <= 0:
                return
            self._persona_reply_count += 1
            if self._persona_reply_count < every or self._persona_evolve_pending:
                return

            dialogue = '\n'.join(self._persona_recent.get(umo, []))
            if not dialogue:
                return

            self._persona_reply_count = 0
            self._persona_evolve_pending = True
            self._spawn_background(self._run_persona_evolution(dialogue, persona_config))
        except Exception as e:
            logger.warning(f"[Fiscok's][persona] 记录人设演化素材失败，不影响主流程: {e}")

    def _append_persona_line(self, umo: str, speaker: str, message: str, cfg: Dict):
        """记录一条人设演化参考对话（单条截断，按配置保留条数）"""
        message = (message or '').strip()
        if not message:
            return
        max_chars = int(cfg.get('review_line_chars', 80) or 0)
        if max_chars > 0 and len(message) > max_chars:
            message = message[:max_chars] + '…'

        lines = self._persona_recent.setdefault(umo, [])
        lines.append(f"{speaker}：{message}")

        max_lines = int(cfg.get('review_recent_lines', 8) or 0)
        if max_lines > 0 and len(lines) > max_lines:
            del lines[:len(lines) - max_lines]

    async def _run_persona_evolution(self, dialogue: str, cfg: Dict):
        """后台调用辅助模型演化自我设定，成功则持久化并即时生效"""
        try:
            max_chars = int(cfg.get('max_chars', 200) or 200)
            result = await evolve_persona(
                self.context,
                provider_id=cfg.get('llm_provider_id', ''),
                current_persona=self._persona_overlay,
                dialogue=dialogue,
                max_chars=max_chars,
            )
            if not result or not result.get('changed'):
                return

            persona = (result.get('persona', '') or '').strip()
            if not persona or persona == self._persona_overlay:
                return

            if self.data_manager.save_evolved_persona(persona, result.get('reason', '')):
                self._persona_overlay = persona
                logger.info(f"[Fiscok's][persona] 人设已演化：{persona[:60]}")
        except Exception as e:
            logger.warning(f"[Fiscok's][persona] 后台人设演化失败，不影响主流程: {e}")
        finally:
            self._persona_evolve_pending = False

    def _schedule_emotion_analysis(self, sender_id: str, nickname: str, message: str, emotion_config: Dict):
        """
        将情绪/好感度分析放入后台异步执行（同一用户串行，避免重复堆积）
        """
        if sender_id in self._emotion_pending:
            return
        self._emotion_pending.add(sender_id)

        async def _run():
            try:
                async with self._emotion_lock:
                    result = await analyze_emotion_state(
                        self.context,
                        provider_id=emotion_config.get('llm_provider_id', ''),
                        nickname=nickname,
                        message=message,
                        current_emotion=self.current_emotion,
                        temperature=emotion_config.get('temperature', 1.2),
                        delta_max=emotion_config.get('delta_max', 5),
                    )
                    if not result:
                        return

                    # 更新模型全局情绪（文字描述）
                    new_emotion = result.get('emotion', '')
                    if new_emotion:
                        self.current_emotion = new_emotion

                    # 更新该用户好感度（数值，JSON 持久化）
                    self.data_manager.update_user_affinity(
                        sender_id,
                        affinity_delta=result.get('affinity_delta', 0),
                        nickname=nickname,
                        min_=emotion_config.get('affinity_min', 0),
                        max_=emotion_config.get('affinity_max', 100),
                    )
            except Exception as e:
                logger.warning(f"[Fiscok's][emotion] 后台情绪/好感度分析失败，不影响主请求: {e}")
            finally:
                self._emotion_pending.discard(sender_id)

        self._spawn_background(_run())

    def _schedule_topic_learning(self, sender_id: str, nickname: str, message: str, topic_config: Dict):
        """
        将话题记忆判定放入后台异步执行（同一用户串行，避免重复堆积）
        """
        if sender_id in self._topic_pending:
            return
        self._topic_pending.add(sender_id)

        async def _run():
            try:
                # 辅助 LLM 判定是否值得保存该话题
                result = await decide_topic_memory(
                    self.context,
                    llm_provider_id=topic_config.get('llm_provider_id', ''),
                    nickname=nickname,
                    dialogue=message,
                )
                if not result:
                    return

                # 计算话题内容的词向量后入库
                vector = await get_text_embedding(
                    self.context,
                    topic_config.get('embedding_provider_id', ''),
                    result.get('content', ''),
                )
                if not vector:
                    logger.warning("[Fiscok's][topic] 话题向量计算失败，跳过入库")
                    return

                self.data_manager.add_topic(result.get('keyword', ''), result.get('content', ''), vector)
            except Exception as e:
                logger.warning(f"[Fiscok's][topic] 后台话题记忆判定失败，不影响主请求: {e}")
            finally:
                self._topic_pending.discard(sender_id)

        self._spawn_background(_run())

    def _build_topic_seed(self, trigger: str, reply: str, track_config: Dict) -> str:
        """
        构造话题种子（触发消息 + 机器人回复），截断以控制判定 token
        """
        seed = f"用户：{trigger}\n我：{reply}" if reply else f"用户：{trigger}"
        max_chars = int(track_config.get('topic_seed_chars', 80) or 0)
        if max_chars > 0 and len(seed) > max_chars:
            seed = seed[:max_chars] + '…'
        return seed

    @staticmethod
    def _append_recent(session: Dict, nickname: str, message: str, track_config: Dict):
        """
        记录最近群聊消息：单条截断，并按配置保留条数
        """
        message = (message or '').strip()
        if not message:
            return

        max_chars = int(track_config.get('recent_line_chars', 60) or 0)
        if max_chars > 0 and len(message) > max_chars:
            message = message[:max_chars] + '…'

        recent = session.setdefault('recent', [])
        recent.append(f"{nickname}：{message}")

        max_lines = int(track_config.get('recent_lines', 4) or 0)
        if max_lines > 0 and len(recent) > max_lines:
            del recent[:len(recent) - max_lines]

    def _end_topic_session(self, umo: str, reason: str):
        """结束话题跟踪会话"""
        if self._topic_sessions.pop(umo, None) is not None:
            logger.info(f"[Fiscok's][track] 结束话题跟踪（{reason}）")

    async def _get_or_create_conversation(self, event: AstrMessageEvent):
        """
        获取当前群聊对话，不存在时创建，用于续聊时复用上下文
        """
        umo = event.unified_msg_origin
        manager = self.context.conversation_manager
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            conversation_id = await manager.new_conversation(
                umo, platform_id=event.get_platform_id()
            )
        return await manager.get_conversation(umo, conversation_id)

    # --- 主动发起聊天 ---

    @staticmethod
    def _random_silence(cfg: Dict, is_private: bool = False) -> float:
        """
        随机生成“冷场多久后尝试开口”的间隔（秒），避免固定节奏。
        私聊使用独立的下限/上限，默认间隔更长，避免打扰
        """
        if is_private:
            min_minutes = float(cfg.get('private_min_silence_minutes', 120) or 0)
            max_minutes = float(cfg.get('private_max_silence_minutes', 360) or 0)
        else:
            min_minutes = float(cfg.get('min_silence_minutes', 30) or 0)
            max_minutes = float(cfg.get('max_silence_minutes', 90) or 0)
        if max_minutes < min_minutes:
            max_minutes = min_minutes
        return random.uniform(min_minutes, max_minutes) * 60.0

    @staticmethod
    def _is_user_allowed(sender_id: str, cfg: Dict) -> bool:
        """私聊主动的用户白名单匹配（QQ 号，逗号分隔；留空表示不启用私聊主动）"""
        if not sender_id:
            return False
        whitelist = str(cfg.get('private_whitelist', '') or '').strip()
        if not whitelist:
            return False
        items = [item.strip() for item in whitelist.replace('，', ',').split(',') if item.strip()]
        return sender_id in items

    def _append_group_line(self, umo: str, nickname: str, message: str, cfg: Dict):
        """
        记录群聊最近一条消息（单条截断、按配置保留条数）
        """
        message = (message or '').strip()
        if not message:
            return

        max_chars = int(cfg.get('recent_line_chars', 80) or 0)
        if max_chars > 0 and len(message) > max_chars:
            message = message[:max_chars] + '…'

        lines = self._group_recent.setdefault(umo, [])
        lines.append(f"{nickname}：{message}")

        max_lines = int(cfg.get('recent_lines', 8) or 0)
        if max_lines > 0 and len(lines) > max_lines:
            del lines[:len(lines) - max_lines]

    @staticmethod
    def _is_group_allowed(umo: str, cfg: Dict) -> bool:
        """群聊白名单匹配（按 umo 或群号子串，留空表示全部）"""
        whitelist = str(cfg.get('group_whitelist', '') or '').strip()
        if not whitelist:
            return True
        items = [item.strip() for item in whitelist.replace('，', ',').split(',') if item.strip()]
        return any(item in umo for item in items)

    @staticmethod
    def _in_active_hours(cfg: Dict) -> bool:
        """是否在允许主动发言的时段（支持跨零点，如 23~7）"""
        start = int(cfg.get('quiet_hours_start', 0) or 0)
        end = int(cfg.get('quiet_hours_end', 7) or 0)
        hour = time.localtime().tm_hour
        if start == end:
            return True
        if start < end:
            return not (start <= hour < end)
        return not (hour >= start or hour < end)

    @staticmethod
    def _describe_now() -> str:
        """当前时间描述（用于提示词，让发言符合作息）"""
        weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        lt = time.localtime()
        hour = lt.tm_hour
        if hour < 6:
            period = '凌晨'
        elif hour < 11:
            period = '上午'
        elif hour < 14:
            period = '中午'
        elif hour < 18:
            period = '下午'
        elif hour < 23:
            period = '晚上'
        else:
            period = '深夜'
        return f"{weekdays[lt.tm_wday]}{period} {lt.tm_hour:02d}:{lt.tm_min:02d}"

    async def _proactive_loop(self):
        """
        主动发起聊天：周期性检查冷场群聊，由模型决定是否开口
        """
        while self.running:
            try:
                cfg = self.config.get('proactive_chat_config', {})
                interval = max(15, int(cfg.get('check_interval_seconds', 60) or 60))
                await asyncio.sleep(interval)
                if not cfg.get('available', False) and not cfg.get('private_available', False):
                    continue
                await self._check_proactive(cfg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Fiscok's][proactive] 主动聊天检查失败: {e}")
                await asyncio.sleep(60)

    async def _check_proactive(self, cfg: Dict):
        """遍历候选群聊：满足冷场、时段、冷却、日上限后，随机尝试开口"""
        if not self._in_active_hours(cfg):
            return

        now = time.time()
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        cooldown = float(cfg.get('cooldown_minutes', 20) or 0) * 60
        probability = float(cfg.get('speak_probability', 0.6) or 0)

        for umo, state in list(self._proactive_states.items()):
            try:
                is_private = bool(state.get('is_private'))
                if is_private:
                    if not cfg.get('private_available', False):
                        continue
                    if not self._is_user_allowed(state.get('sender_id', ''), cfg):
                        continue
                    min_silence = float(cfg.get('private_min_silence_minutes', 120) or 0) * 60
                    daily_limit = int(cfg.get('private_daily_limit', 3) or 0)
                else:
                    if not cfg.get('available', False):
                        continue
                    if not self._is_group_allowed(umo, cfg):
                        continue
                    min_silence = float(cfg.get('min_silence_minutes', 30) or 0) * 60
                    daily_limit = int(cfg.get('daily_limit_per_group', 6) or 0)

                if state.get('daily_date') != today:
                    state['daily_date'] = today
                    state['daily_count'] = 0
                if daily_limit > 0 and state.get('daily_count', 0) >= daily_limit:
                    continue

                last_active = max(
                    self._group_last_activity.get(umo, 0),
                    state.get('last_spoke_at', 0),
                )
                if now - last_active < min_silence:
                    continue
                if now < state.get('earliest_try', 0):
                    continue
                if cooldown > 0 and now - state.get('last_spoke_at', 0) < cooldown:
                    continue

                # 冷场满足后也不是必然开口，保留随机性
                if probability > 0 and random.random() > probability:
                    state['earliest_try'] = now + self._random_silence(cfg, is_private) * random.uniform(0.5, 1.0)
                    continue

                text = await self._generate_proactive_text(umo, state, cfg)
                if not text:
                    state['earliest_try'] = now + self._random_silence(cfg, is_private)
                    continue

                if not await self._send_proactive(umo, text, cfg, is_private):
                    state['earliest_try'] = now + self._random_silence(cfg, is_private)
                    continue

                spoke_at = time.time()
                state['last_spoke_at'] = spoke_at
                state['daily_count'] = state.get('daily_count', 0) + 1
                state['earliest_try'] = spoke_at + self._random_silence(cfg, is_private)

                last_messages = state.setdefault('last_messages', [])
                last_messages.append(text)
                max_last = int(cfg.get('last_messages', 3) or 0)
                if max_last > 0 and len(last_messages) > max_last:
                    del last_messages[:len(last_messages) - max_last]
            except Exception as e:
                logger.warning(f"[Fiscok's][proactive] 处理群聊 {umo} 主动发言失败: {e}")

    async def _generate_proactive_text(self, umo: str, state: Dict, cfg: Dict) -> str:
        """结合人格、情绪、好感度、时间与会话记录生成一条主动消息"""
        recent = list(self._group_recent.get(umo, []))
        if not recent:
            return ''

        is_private = bool(state.get('is_private'))
        persona_prompt = ''
        try:
            persona_obj = await self.context.persona_manager.get_default_persona_v3(umo)
            if isinstance(persona_obj, dict):
                persona_prompt = persona_obj.get('prompt', '') or ''
        except Exception:
            persona_prompt = ''

        # 带上当前情绪状态，让主动发言的语气和近期状态一致
        if self.current_emotion:
            persona_prompt = f"{persona_prompt}\n\n你现在的心情：{self.current_emotion}".strip()

        # 带上演化出的自我设定（开启人设演化时生效）
        if self._persona_overlay and self.config.get('persona_evolution_config', {}).get('available', False):
            persona_prompt = f"{persona_prompt}\n\n你的自我设定：{self._persona_overlay}".strip()

        # 私聊时带上对该用户的好感度，让亲近程度和关系一致
        if is_private and state.get('sender_id'):
            try:
                affinity = self.data_manager.get_user_affinity(
                    state['sender_id'], state.get('nickname')
                )
                persona_prompt = f"{persona_prompt}\n\n你对 TA 的好感度：{affinity}/100".strip()
            except Exception:
                pass

        text = await generate_proactive_message(
            self.context,
            provider_id=cfg.get('llm_provider_id', ''),
            persona_prompt=persona_prompt,
            time_desc=self._describe_now(),
            recent=recent,
            last_messages=list(state.get('last_messages', [])),
            nickname=state.get('nickname', ''),
            is_private=is_private,
        )
        if not text:
            return ''

        max_chars = int(cfg.get('max_chars', 120) or 0)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + '…'
        return text

    async def _send_proactive(self, umo: str, text: str, cfg: Dict, is_private: bool = False) -> bool:
        """发送主动消息；开启分段发送时按换行拆成多条，更像真人。
        返回是否发送成功"""
        seg_cfg = self.config.get('segmented_parser_config', {})
        if seg_cfg.get('available', False):
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            interval = float(seg_cfg.get('send_interval', 0.5) or 0)
        else:
            lines = [text]
            interval = 0

        sent = 0
        for index, line in enumerate(lines):
            if index and interval > 0:
                await asyncio.sleep(interval)
            if await self.context.send_message(umo, MessageChain().message(line)):
                sent += 1

        if not sent:
            logger.warning("[Fiscok's][proactive] 主动发言发送失败（未找到对应平台）")
            return False

        logger.info(f"[Fiscok's][proactive] 主动发言：{text[:60]}")
        self._group_last_activity[umo] = time.time()
        self._append_group_line(umo, '我', text, cfg)

        # 若开启了话题跟踪，把这次主动发言也作为一个话题起点（话题跟踪仅针对群聊）
        track_config = self.config.get('topic_tracking_config', {})
        if not is_private and track_config.get('available', False):
            self._start_topic_session(umo, text, list(self._group_recent.get(umo, [])), track_config)

        return True

    def _start_topic_session(self, umo: str, topic: str, recent: List[str], track_config: Dict):
        """以机器人发言为起点开启话题会话"""
        now = time.time()
        max_chars = int(track_config.get('topic_seed_chars', 80) or 0)
        if max_chars > 0 and len(topic) > max_chars:
            topic = topic[:max_chars] + '…'
        self._topic_sessions[umo] = {
            'topic': topic,
            'started_at': now,
            'last_active': now,
            'auto_rounds': 0,
            'idle_rounds': 0,
            'recent': list(recent),
            'lock': asyncio.Lock(),
        }
        logger.info(f"[Fiscok's][track] 由主动发言开启话题跟踪：{topic[:60]}")

    def _spawn_background(self, coro):
        """将协程放入后台执行，并持有引用避免被 GC 回收"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _extract_emoji_urls(self, event: AstrMessageEvent) -> List[str]:
        """
        从消息中提取表情包（sub_type == 1 的图片）地址（同步执行）
        """
        raw_message = event.message_obj.raw_message

        # 获取消息组件列表
        message_parts = None
        if raw_message and hasattr(raw_message, 'message'):
            message_parts = raw_message.message
        elif event.message_obj and hasattr(event.message_obj, 'message'):
            message_parts = event.message_obj.message
        elif isinstance(raw_message, list):
            message_parts = raw_message

        if not message_parts:
            return []

        urls: List[str] = []
        for message_part in message_parts:
            # 检测表情包类型
            if isinstance(message_part, dict):
                msg_type = message_part.get("type")
                msg_data = message_part.get("data", {})
                is_emoji = msg_type == "image" and msg_data.get("sub_type") == 1
                image_url = msg_data.get("url", "") if is_emoji else ""
            else:
                msg_type = getattr(message_part, 'type', None)
                msg_data = getattr(message_part, 'data', {})
                sub_type = None
                if isinstance(msg_data, dict):
                    sub_type = msg_data.get("sub_type")
                elif hasattr(msg_data, 'sub_type'):
                    sub_type = getattr(msg_data, 'sub_type', None)
                if sub_type is None:
                    sub_type = getattr(message_part, 'sub_type', None)

                is_emoji = msg_type == "image" and sub_type == 1
                image_url = ''
                if is_emoji:
                    if isinstance(msg_data, dict):
                        image_url = msg_data.get("url", "")
                    else:
                        image_url = getattr(msg_data, 'url', '') or getattr(message_part, 'url', '')

            if is_emoji and image_url:
                urls.append(image_url)

        return urls

    async def _learn_meme_from_message(self, image_urls: List[str], source: str, meme_config: Dict):
        """
        从消息中学习表情包：下载、调用 LLM 生成描述、入库（后台异步执行）
        """
        try:
            for image_url in image_urls:
                logger.info(f"[Fiscok's][meme] 检测到表情包: {image_url}")

                # 下载图片到临时目录
                temp_dir = self.data_manager.meme_library_root / 'temp'
                temp_dir.mkdir(parents=True, exist_ok=True)

                # 生成临时文件名
                import time
                temp_filename = f"temp_{int(time.time() * 1000)}.jpg"
                temp_path = temp_dir / temp_filename

                # 下载图片
                success = await self._download_image(image_url, temp_path)
                if not success:
                    logger.warning(f"[Fiscok's][meme] 下载表情包失败: {image_url}")
                    continue

                # 调用 LLM 生成描述
                provider_id = meme_config.get('multimodal_provider_id', '')
                description_result = await generate_meme_description(
                    str(temp_path),
                    self.context,
                    provider_id
                )

                if not description_result:
                    logger.warning("[Fiscok's][meme] LLM 生成描述失败，跳过入库")
                    # 清理临时文件
                    if temp_path.exists():
                        temp_path.unlink()
                    continue

                # 添加到表情库
                meme_id = self.data_manager.add_meme(
                    image_path=str(temp_path),
                    description=description_result.get('description', ''),
                    tags=description_result.get('tags', []),
                    emotion=description_result.get('emotion', 'funny'),
                    source=source
                )

                # 清理临时文件
                if temp_path.exists():
                    temp_path.unlink()

                if meme_id:
                    logger.info(f"[Fiscok's][meme] 成功入库表情包: {meme_id}")
                else:
                    logger.warning("[Fiscok's][meme] 表情包入库失败")

        except Exception as e:
            logger.error(f"[Fiscok's][meme] 学习表情包时出错: {e}", exc_info=True)

    async def _download_image(self, url: str, save_path: Path) -> bool:
        """
        下载图片到指定路径

        Args:
            url: 图片 URL
            save_path: 保存路径

        Returns:
            是否成功
        """
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"[Fiscok's][meme] 下载图片失败，状态码: {response.status}")
                        return False

                    async with aiofiles.open(save_path, mode="wb") as f:
                        async for chunk in response.content.iter_chunked(1024):
                            await f.write(chunk)

                    return True
        except Exception as e:
            logger.error(f"[Fiscok's][meme] 下载图片异常: {e}", exc_info=True)
            return False

    @filter.on_llm_response()
    async def on_llm_response_topic_tracking_hook(self, event: AstrMessageEvent, resp):
        """
        机器人发言后开启/刷新话题会话：被 @/唤醒触发时以这次发言开启新话题，
        续聊时仅刷新活跃时间
        """
        track_config = self.config.get('topic_tracking_config', {})
        if not track_config.get('available', False):
            return

        if not event.get_group_id():
            return

        try:
            reply = (getattr(resp, 'completion_text', '') or '').strip()
            trigger = (event.message_str or '').strip()
            if not reply and not trigger:
                return

            umo = event.unified_msg_origin
            now = time.time()
            session = self._topic_sessions.get(umo)

            if session is None or event.is_at_or_wake_command:
                # 被 @/唤醒视为开启新话题：覆盖旧会话
                session = {
                    'topic': self._build_topic_seed(trigger, reply, track_config),
                    'started_at': now,
                    'last_active': now,
                    'auto_rounds': 0,
                    'idle_rounds': 0,
                    'recent': [],
                    'lock': asyncio.Lock(),
                }
                self._topic_sessions[umo] = session
                self._append_recent(session, event.get_sender_name() or '群友', trigger, track_config)
                self._append_recent(session, '我', reply, track_config)
                logger.info(f"[Fiscok's][track] 开启话题跟踪：{session['topic'][:60]}")
            else:
                # 续聊回复：刷新活跃时间，并记录自己的发言
                session['last_active'] = now
                self._append_recent(session, '我', reply, track_config)
        except Exception as e:
            logger.warning(f"[Fiscok's][track] 更新话题会话失败，不影响主流程: {e}")

    @filter.on_llm_response()
    async def on_llm_response_proactive_hook(self, event: AstrMessageEvent, resp):
        """
        主动发起聊天：记录机器人发言，并把“机器人发过言”的群纳入主动聊天候选
        """
        proactive_config = self.config.get('proactive_chat_config', {})
        umo = event.unified_msg_origin
        is_private = not event.get_group_id()
        if is_private:
            if not proactive_config.get('private_available', False):
                return
            if not self._is_user_allowed(event.get_sender_id(), proactive_config):
                return
        elif not proactive_config.get('available', False):
            return

        try:
            reply = (getattr(resp, 'completion_text', '') or '').strip()
            now = time.time()
            self._group_last_activity[umo] = now

            if reply:
                self._append_group_line(umo, '我', reply, proactive_config)

            if not reply:
                return

            state = self._proactive_states.get(umo)
            if state is None:
                # 只有机器人真正发过言的会话才纳入主动聊天，避免在陌生群里突兀发言
                self._proactive_states[umo] = {
                    'is_private': is_private,
                    'sender_id': event.get_sender_id() if is_private else '',
                    'nickname': event.get_sender_name() if is_private else '',
                    'earliest_try': now + self._random_silence(proactive_config, is_private),
                    'last_spoke_at': 0.0,
                    'daily_date': '',
                    'daily_count': 0,
                    'last_messages': [],
                }
                logger.info(
                    f"[Fiscok's][proactive] {'私聊用户' if is_private else '群'}已纳入主动聊天候选：{umo}"
                )
            elif is_private:
                # 刷新私聊用户信息（昵称可能变化）
                state['sender_id'] = event.get_sender_id()
                state['nickname'] = event.get_sender_name()
        except Exception as e:
            logger.warning(f"[Fiscok's][proactive] 记录群活跃状态失败，不影响主流程: {e}")

    @filter.on_llm_response()
    async def on_llm_response_hook(self, event: AstrMessageEvent, resp):
        """
        LLM 响应后处理：解析占位符，清理文本和消息链中的占位符，标记待发送的表情包
        """
        meme_config = self.config.get('meme_config', {})
        if not meme_config.get('meme_available', False):
            return

        try:
            completion_text = resp.completion_text
            if not completion_text:
                return

            placeholder_tag = meme_config.get('placeholder_tag', 'meme')
            # 匹配占位符 [meme:情绪描述]
            pattern = rf'\[{placeholder_tag}:(.+?)\]'
            matches = re.findall(pattern, completion_text)

            if not matches:
                return

            logger.info(f"[Fiscok's][meme] 发现 {len(matches)} 个表情包占位符: {matches}")

            memes_to_send = []
            clean_text = completion_text

            for emotion in matches:
                meme = self.data_manager.find_meme_by_emotion(emotion.strip())
                if not meme:
                    logger.info(f"[Fiscok's][meme] 未找到匹配情绪 '{emotion}' 的表情包")
                    continue

                meme_path = self.data_manager.meme_library_root / meme.get('filename', '')
                if not meme_path.exists():
                    logger.warning(f"[Fiscok's][meme] 表情包文件不存在: {meme_path}")
                    continue

                memes_to_send.append(str(meme_path))
                # 从文本中移除占位符
                clean_text = clean_text.replace(f"[{placeholder_tag}:{emotion}]", "")
                logger.info(f"[Fiscok's][meme] 已标记表情包待发送: {meme_path}")

            # 清理文本中多余的空行
            clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
            resp.completion_text = clean_text

            # 同步清理 result_chain 中的 Plain 组件
            if hasattr(resp, 'result_chain') and resp.result_chain:
                chain = resp.result_chain.chain if hasattr(resp.result_chain, 'chain') else []
                for component in chain:
                    if isinstance(component, Plain):
                        for emotion in matches:
                            component.text = component.text.replace(f"[{placeholder_tag}:{emotion}]", "")
                        component.text = re.sub(r'\n{3,}', '\n\n', component.text).strip()

            # 保存待发送表情包路径到事件 extra
            if memes_to_send:
                event.set_extra("_memes_to_attach", memes_to_send)

        except Exception as e:
            logger.error(f"[Fiscok's][meme] 处理 LLM 响应时出错: {e}", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result_hook(self, event: AstrMessageEvent):
        """
        发送消息前装饰：清理消息链中的占位符残留，并延迟单独发送表情包图片
        """
        memes_paths = event.get_extra("_memes_to_attach", [])
        if not memes_paths:
            return

        try:
            # 再次清理消息链中的占位符残留（防御性检查）
            result = event.get_result()
            if result and result.chain:
                placeholder_tag = self.config.get('meme_config', {}).get('placeholder_tag', 'meme')
                pattern = rf'\[{placeholder_tag}:.+?\]'
                for component in result.chain:
                    if isinstance(component, Plain):
                        component.text = re.sub(pattern, '', component.text).strip()
                        component.text = re.sub(r'\n{3,}', '\n\n', component.text).strip()

            # 延迟单独发送表情包图片（确保文本消息先到达）
            umo = event.unified_msg_origin
            for meme_path in memes_paths:
                asyncio.create_task(self._send_meme_separately(umo, meme_path))

            # 清除 extra 避免重复发送
            event.set_extra("_memes_to_attach", [])

        except Exception as e:
            logger.error(f"[Fiscok's][meme] 装饰消息链时出错: {e}", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result_split_hook(self, event: AstrMessageEvent):
        """
        发送前将 LLM 回复按换行符拆分为多条消息分别发送（跳过空白行）
        定义在表情包装饰钩子之后，确保先清理占位符再拆分
        """
        segmented_config = self.config.get('segmented_parser_config', {})
        if not segmented_config.get('available', False):
            return

        result = event.get_result()
        if result is None or not result.is_model_result():
            return

        # 收集文本段（按换行拆分、跳过空白行）与非文本组件
        plain_segments = []
        other_comps = []
        if result.chain:
            for component in result.chain:
                if isinstance(component, Plain):
                    for line in component.text.split("\n"):
                        line = line.strip()
                        if line:
                            plain_segments.append(line)
                else:
                    other_comps.append(component)

        if len(plain_segments) + len(other_comps) <= 1:
            return

        try:
            umo = event.unified_msg_origin
            # 先发送非文本组件（如图片/表情包）
            if other_comps:
                await self.context.send_message(umo, MessageChain(chain=other_comps))

            # 再逐行发送文本
            send_interval = segmented_config.get('send_interval', 0.5)
            for line in plain_segments:
                await self.context.send_message(umo, MessageChain(chain=[Plain(line)]))
                if send_interval > 0:
                    await asyncio.sleep(send_interval)

            # 原消息链不再重复发送
            event.clear_result()
        except Exception as e:
            logger.error(f"[Fiscok's][segmented] 分段发送失败: {e}", exc_info=True)

    async def _send_meme_separately(self, umo: str, meme_path: str):
        """
        延迟发送表情包图片（单独一条消息），确保文本消息先到达
        """
        try:
            await asyncio.sleep(0.5)
            path = Path(meme_path)
            if path.exists():
                chain = MessageChain(chain=[Image.fromFileSystem(str(path))])
                await self.context.send_message(umo, chain)
                logger.info(f"[Fiscok's][meme] 已单独发送表情包: {meme_path}")
            else:
                logger.warning(f"[Fiscok's][meme] 表情包文件不存在，跳过: {meme_path}")
        except Exception as e:
            logger.error(f"[Fiscok's][meme] 单独发送表情包失败: {e}", exc_info=True)

    # 临时测试用指令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('pull_cache_test', alias={'拉取测试'})
    async def test_command_1(self, event: AstrMessageEvent):
        """
        这是一个测试指令，用于验证推特缓存功能
        """
        await fetch_twitter_data('aimi_sound', self.data_manager, self.rssHub_full_url)
        yield event.plain_result("已执行测试指令，检查日志以验证推特")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('push_cache_test', alias={'推送测试'})
    async def test_command_2(self, event: AstrMessageEvent):
        """
        这是一个测试指令，用于验证推特定时推送功能
        """
        await self.twitter_scheduled_push()
        yield event.plain_result("已执行测试指令，检查对应群聊以验证推送内容")

    # --- Bilibili视频发布统计（火星救援） ---
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bili_video_count(self, event: AstrMessageEvent):
        """
        解析 Bilibili 链接并判断是否在该群聊被发送过
        仅处理主动发送的消息，忽略引用和转发消息
        """
        # 检查消息是否包含引用或转发组件
        if event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, (Reply, Forward)):
                    return  # 忽略引用和转发消息

        bvid = await get_bvid(event)
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        sender_nickname = event.get_sender_name()

        if bvid and group_id and sender_id:
            video_storage = self.data_manager.get_bili_video_storage(group_id, bvid)
            if video_storage:
                first_sharer = video_storage['first_sharer']
                timestamp = video_storage['timestamp']
                count = video_storage['count']

                response_message = (f'本视频已经被{first_sharer}于{timestamp}发布过啦！'
                                    f'目前已经被群友发布了{count}次，又要重复吗，这绝望的轮回...')
                yield event.plain_result(response_message)
            else:
                self.data_manager.update_bili_video_storage(group_id, sender_nickname, sender_id, bvid)
                response_message = (f'还是第一次在这里看到这个视频呢，'
                                    f'为什么要和我说这个...')
                yield event.plain_result(response_message)
        else:
            # 默认通行
            return

    # --- 推特缓存更新 ---
    async def twitter_cache_update(self):
        """
        定期从 RSSHub 获取订阅的推特账号的最新动态，并更新缓存
        """
        while self.running:
            twitter_config = self.config.get('twitter_subscription_config', {})
            interval = twitter_config.get("twitter_push_cache_time", 1)
            await asyncio.sleep(3600 * interval)  # 每小时更新一次
            if not twitter_config.get("twitter_subscription_available"):
                continue
            subscriptions = self.data_manager.get_twitter_subscriptions()
            if not subscriptions:
                continue
            await self._refresh_twitter_cache(subscriptions, twitter_config)

    async def _refresh_twitter_cache(self, subscriptions: List[str], twitter_config: Dict):
        """
        串行拉取各订阅账号的最新动态：账号之间按配置间隔等待，并复用同一个 HTTP 会话
        """
        fetch_interval = twitter_config.get("twitter_fetch_interval", 60)
        logger.info(f"[Fiscok's][twitter_push]正在更新推特缓存，共 {len(subscriptions)} 个订阅")
        async with aiohttp.ClientSession() as session:
            for idx, twitter_id in enumerate(subscriptions):
                if idx > 0 and fetch_interval > 0:
                    await asyncio.sleep(fetch_interval)
                logger.info(f"[Fiscok's][twitter_push]正在拉取推特账号 @{twitter_id} 的最新动态")
                await fetch_twitter_data(twitter_id, self.data_manager, self.rssHub_full_url, session=session)

    # --- 推特定时推送 ---
    async def twitter_scheduled_push(self):
        """
        将未推送的推特缓存推送到对应的群聊
        """
        logger.info(f"[Fiscok's][twitter_push]正在执行定时推送任务")
        subscriptions = self.data_manager.get_all_twitter_subscriptions()
        unified_msg_origins = self.data_manager.get_umo()
        push_interval = self.config.get('twitter_subscription_config', {}).get("twitter_push_interval", 5)
        logger.info(f"[Fiscok's][twitter_push]当前订阅列表: {subscriptions}")
        first_send = True

        for subscription in subscriptions:
            logger.info(f"[Fiscok's][twitter_push]正在处理订阅 @{subscription['twitter_id']} 的推送")
            alias = subscription['alias'] if subscription['alias'] else subscription['twitter_id']
            twitter_id = subscription['twitter_id']
            group_ids = subscription['group_ids']

            forward_node = self._quote_info_create(
                alias=alias,
                account_id=twitter_id,
                # 只读取用于转发的少量缓存内容，避免把该账号全部未推送缓存都载入内存
                cache_getter=lambda account_id: self.data_manager.get_twitter_cache(account_id, limit=10),
                platform_name="动态"
            )
            if forward_node is None:
                logger.info(f"[Fiscok's][twitter_push]未找到 @{twitter_id} 的有效缓存，跳过推送")
                continue
            message_chain = MessageChain(chain=[forward_node])

            for group_id in group_ids:
                umo = unified_msg_origins.get(group_id)
                # 仅在两次发送之间节流，避免无谓地等待首条消息
                if not first_send and push_interval > 0:
                    await asyncio.sleep(push_interval)
                first_send = False
                logger.info(f"[Fiscok's][twitter_push]正在向群 {group_id} 推送 @{twitter_id} 的最新动态")
                res = await self.context.send_message(umo, message_chain)
                logger.info(f"[Fiscok's][twitter_push]向群 {group_id} 推送 @{twitter_id} 的结果: {res}")

    # --- 推特订阅推送指令组 ---
    @filter.command_group('twitter_manager', alias={'推特管理'})
    def twitter_manager(self):
        pass

    @twitter_manager.command('subscribe', alias={'订阅'})
    async def twitter_subscribe(self, event: AstrMessageEvent, twitter_id: str, alias: str = None):
        if not re.match(r'^[A-Za-z0-9_]{1,15}$', twitter_id):
            yield event.plain_result("无效的 Twitter ID...也许你应该再看看")
            return

        flag = self.data_manager.add_twitter_subscription(
           event.get_group_id(),
           twitter_id,
           alias,
           event.unified_msg_origin
        )
        if not flag:
            yield event.plain_result(f"订阅失败，可能是因为已经订阅了 @{twitter_id}，或者数据存储出现问题")
            return
        yield event.plain_result(f"已订阅推特账号 @{twitter_id}({alias if alias else '无'})，请等待更新推送")

    @twitter_manager.command('unsubscribe', alias={'取消订阅'})
    async def twitter_unsubscribe(self, event: AstrMessageEvent, twitter_id: str):
        self.data_manager.remove_twitter_subscription(event.get_group_id(), twitter_id)
        yield event.plain_result(f"已取消推特订阅 @{twitter_id}，不再接收更新推送")

    @twitter_manager.command('list', alias={'订阅列表'})
    async def twitter_list(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        subscriptions = self.data_manager.get_group_twitter_subscriptions(group_id)
        if not subscriptions:
            yield event.plain_result("当前没有订阅任何推特账号")
            return
        response_message = "当前订阅的推特账号列表：\n"
        for sub in subscriptions:
            response_message += f"- @{sub['twitter_id']} ({sub['alias'] if sub['alias'] else '无'})\n"
        yield event.plain_result(response_message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @twitter_manager.command('update_cookie', alias={'更新Cookie'})
    async def twitter_update_cookie(self, event: AstrMessageEvent, auth_token: str, ct0: str):
        env_path = "/rsshub/.env"  # 容器内的挂载路径

        with open(env_path, "w") as f:
            f.write(f"TWITTER_AUTH_TOKEN={auth_token}\n")
            f.write(f"TWITTER_CT0={ct0}\n")

        # 同步更新统一 cookie 文件
        self.cookies['twitter'] = {"auth_token": auth_token, "ct0": ct0}
        self._save_cookies()

        subprocess.run(["docker", "restart", "rsshub"])
        logger.info("已更新 Twitter Cookie 并重启 RSSHub，新的订阅推送将在几分钟内生效")
        yield event.plain_result("已更新 Twitter Cookie 并重启 RSSHub，新的订阅推送将在几分钟内生效")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @twitter_manager.command('check_available', alias={'检查连接状态'})
    async def twitter_check_available(self, event: AstrMessageEvent):
        status = await check_availability(self.rssHub_full_url)
        if status:
            yield event.plain_result("RSSHub 服务连接正常，可以正常获取推特更新")
        else:
            yield event.plain_result("RSSHub 服务连接异常，可能需要更新cookies")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @twitter_manager.command('trigger_cache_update', alias={'手动缓存更新'})
    async def twitter_trigger_cache_update(self, event: AstrMessageEvent):
        twitter_config = self.config.get('twitter_subscription_config', {})
        if twitter_config.get("twitter_subscription_available"):
            subscriptions = self.data_manager.get_twitter_subscriptions()
            if subscriptions:
                await self._refresh_twitter_cache(subscriptions, twitter_config)
        yield event.plain_result("已手动触发推特缓存更新，请检查日志以验证更新过程")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @twitter_manager.command('trigger_scheduled_push', alias={'手动推送'})
    async def twitter_trigger_scheduled_push(self, event: AstrMessageEvent):
        await self.twitter_scheduled_push()
        yield event.plain_result("已手动触发推特定时推送，请检查对应群聊以验证推送内容")

    # --- 图库管理指令组 ---
    @filter.command_group('gallery_manager', alias={'图库管理'})
    def gallery_manager(self):
        pass

    # --- 话题记忆管理指令组 ---
    @filter.command_group('topic_memory_manager', alias={'话题记忆管理'})
    def topic_memory_manager(self):
        pass

    @topic_memory_manager.command('list', alias={'列表'})
    async def topic_memory_list(self, event: AstrMessageEvent, limit: int = 20):
        topics = self.data_manager.get_all_topics()
        if not topics:
            yield event.plain_result("话题记忆库为空")
            return
        recent = topics[-limit:]
        lines = [f"话题记忆共 {len(topics)} 条，最近 {len(recent)} 条："]
        for topic in recent:
            lines.append(f"- {topic.get('keyword', '')} | {topic.get('timestamp', '')}")
        yield event.plain_result("\n".join(lines))

    @topic_memory_manager.command('count', alias={'数量'})
    async def topic_memory_count(self, event: AstrMessageEvent):
        max_topics = self.config.get('topic_memory_config', {}).get('max_topics', 300)
        yield event.plain_result(f"当前话题记忆 {self.data_manager.get_topic_count()} 条，上限 {max_topics} 条")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @topic_memory_manager.command('clear', alias={'清空'})
    async def topic_memory_clear(self, event: AstrMessageEvent):
        self.data_manager.clear_topics()
        yield event.plain_result("已清空话题记忆库")

    # --- 人设演化管理指令组 ---
    @filter.command_group('persona_manager', alias={'人设管理'})
    def persona_manager(self):
        pass

    @persona_manager.command('show', alias={'查看'})
    async def persona_show(self, event: AstrMessageEvent):
        record = self.data_manager.get_evolved_persona()
        persona = (record.get('persona', '') or '').strip()
        if not persona:
            yield event.plain_result("当前还没有演化出自我设定（开启人设演化并聊天一段时间后会自动形成）")
            return
        lines = ["当前自我设定：", persona]
        if record.get('updated_at'):
            lines.append(f"更新时间：{record['updated_at']}")
        if record.get('reason'):
            lines.append(f"最近调整原因：{record['reason']}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @persona_manager.command('evolve', alias={'演化一次'})
    async def persona_evolve(self, event: AstrMessageEvent):
        persona_config = self.config.get('persona_evolution_config', {})
        dialogue = '\n'.join(self._persona_recent.get(event.unified_msg_origin, []))
        if not dialogue:
            yield event.plain_result("当前会话的参考素材为空，先聊几句再试")
            return
        result = await evolve_persona(
            self.context,
            provider_id=persona_config.get('llm_provider_id', ''),
            current_persona=self._persona_overlay,
            dialogue=dialogue,
            max_chars=int(persona_config.get('max_chars', 200) or 200),
        )
        if not result:
            yield event.plain_result("演化失败（模型不可用或返回内容无法解析），请查看日志")
            return
        if not result.get('changed'):
            yield event.plain_result("模型认为当前设定无需调整")
            return
        persona = (result.get('persona', '') or '').strip()
        if not persona:
            yield event.plain_result("模型没有返回有效的自我设定")
            return
        if self.data_manager.save_evolved_persona(persona, result.get('reason', '')):
            self._persona_overlay = persona
            yield event.plain_result(f"人设已更新：\n{persona}")
        else:
            yield event.plain_result("保存人设失败，请查看日志")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @persona_manager.command('reset', alias={'重置'})
    async def persona_reset(self, event: AstrMessageEvent):
        self.data_manager.reset_evolved_persona()
        self._persona_overlay = ''
        self._persona_recent.clear()
        self._persona_reply_count = 0
        yield event.plain_result("已重置自我设定，回到只使用原生人设的状态")

    # --- Instagram 缓存更新 ---
    async def instagram_cache_update(self):
        """
        定期拉取订阅的 Instagram 账号的最新内容并更新缓存
        """
        while self.running:
            ins_config = self.config.get('instagram_subscription_config', {})
            interval = ins_config.get('instagram_fetch_interval', 1)
            await asyncio.sleep(interval * 3600)
            if not ins_config.get('instagram_subscription_available', False):
                continue

            # 检查 cookies 有效性
            if not await check_instagram_access(self.ins_loader):
                logger.warning("[Fiscok's][instagram] Instagram cookies 已失效，请更新 cookies.json")
                # 尝试重新加载 cookies
                self.cookies = self._load_cookies()
                ins_cookies = self.cookies.get('instagram', {})
                self.ins_loader = create_loader(ins_cookies)
                if not self.ins_loader or not await check_instagram_access(self.ins_loader):
                    logger.error("[Fiscok's][instagram] Instagram cookies 仍然无效，跳过本次更新")
                    continue
                logger.info("[Fiscok's][instagram] Instagram cookies 已重新加载")

            subscriptions = self.data_manager.get_instagram_subscriptions()
            logger.info(f"[Fiscok's][instagram] 正在更新 Instagram 缓存，共 {len(subscriptions)} 个订阅")

            for username in subscriptions:
                logger.info(f"[Fiscok's][instagram] 正在拉取 @{username} 的最新内容")
                await asyncio.sleep(180)  # 请求间隔
                await fetch_instagram_posts(self.ins_loader, username, self.data_manager)
                if ins_config.get('instagram_fetch_stories', True):
                    await asyncio.sleep(180)
                    await fetch_instagram_stories(self.ins_loader, username, self.data_manager)

    # --- Instagram 定时推送 ---
    async def instagram_scheduled_push(self):
        """
        将未推送的 Instagram 缓存推送到对应的群聊
        """
        logger.info("[Fiscok's][instagram] 正在执行 Instagram 定时推送任务")
        subscriptions = self.data_manager.get_all_instagram_subscriptions()
        unified_msg_origins = self.data_manager.get_instagram_umo()

        for subscription in subscriptions:
            username = subscription['username']
            alias = subscription['alias'] if subscription['alias'] else username
            group_ids = subscription['group_ids']

            forward_node = self._instagram_quote_info_create(alias, username)
            if forward_node is None:
                logger.info(f"[Fiscok's][instagram] 未找到 @{username} 的有效缓存，跳过推送")
                continue
            message_chain = MessageChain(chain=[forward_node])

            for group_id in group_ids:
                umo = unified_msg_origins.get(group_id)
                logger.info(f"[Fiscok's][instagram] 正在向群 {group_id} 推送 @{username} 的最新内容")
                await asyncio.sleep(20)
                res = await self.context.send_message(umo, message_chain)
                logger.info(f"[Fiscok's][instagram] 向群 {group_id} 推送 @{username} 的结果: {res}")

    def _instagram_quote_info_create(self, alias: str, username: str) -> Nodes | None:
        """
        构建 Instagram 推送的转发消息
        """
        return self._quote_info_create(
            alias=alias,
            account_id=username,
            cache_getter=self.data_manager.get_instagram_cache,
            platform_name="Instagram 内容",
            text_fallback=True
        )

    # --- Instagram 订阅指令组 ---
    @filter.command_group('instagram_manager', alias={'ins管理'})
    def instagram_manager(self):
        pass

    @instagram_manager.command('subscribe', alias={'订阅'})
    async def instagram_subscribe(self, event: AstrMessageEvent, username: str, alias: str = None):
        flag = self.data_manager.add_instagram_subscription(
            event.get_group_id(),
            username,
            alias,
            event.unified_msg_origin
        )
        if not flag:
            yield event.plain_result(f"订阅失败，可能是因为已经订阅了 @{username}，或者数据存储出现问题")
            return
        yield event.plain_result(f"已订阅 Instagram 账号 @{username}({alias if alias else '无'})，请等待更新推送")

    @instagram_manager.command('unsubscribe', alias={'取消订阅'})
    async def instagram_unsubscribe(self, event: AstrMessageEvent, username: str):
        self.data_manager.remove_instagram_subscription(event.get_group_id(), username)
        yield event.plain_result(f"已取消 Instagram 订阅 @{username}，不再接收更新推送")

    @instagram_manager.command('list', alias={'订阅列表'})
    async def instagram_list(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        subscriptions = self.data_manager.get_group_instagram_subscriptions(group_id)
        if not subscriptions:
            yield event.plain_result("当前没有订阅任何 Instagram 账号")
            return
        response_message = "当前订阅的 Instagram 账号列表：\n"
        for sub in subscriptions:
            response_message += f"- @{sub['username']} ({sub['alias'] if sub['alias'] else '无'})\n"
        yield event.plain_result(response_message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @instagram_manager.command('check_cookies', alias={'检查cookies'})
    async def instagram_check_cookies(self, event: AstrMessageEvent):
        if not self.ins_loader:
            yield event.plain_result("Instagram 功能未启用，请先配置 cookies.json 中的 instagram cookies")
            return
        status = await check_instagram_access(self.ins_loader)
        if status:
            yield event.plain_result("Instagram cookies 有效")
        else:
            yield event.plain_result("Instagram cookies 已失效，请更新 cookies.json 中的 instagram cookies")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @instagram_manager.command('reload_cookies', alias={'重载cookies'})
    async def instagram_reload_cookies(self, event: AstrMessageEvent):
        self.cookies = self._load_cookies()
        ins_cookies = self.cookies.get('instagram', {})
        self.ins_loader = create_loader(ins_cookies)
        if self.ins_loader:
            yield event.plain_result("Instagram cookies 已重新加载")
        else:
            yield event.plain_result("Instagram cookies 加载失败，请检查 cookies.json")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @instagram_manager.command('update_cookies', alias={'更新cookies'})
    async def instagram_update_cookies(self, event: AstrMessageEvent, sessionid: str, ds_user_id: str, csrftoken: str):
        """更新 Instagram cookies"""
        self.cookies['instagram'] = {
            "sessionid": sessionid,
            "ds_user_id": ds_user_id,
            "csrftoken": csrftoken
        }
        self._save_cookies()
        self.ins_loader = create_loader(self.cookies['instagram'])
        if self.ins_loader:
            yield event.plain_result("Instagram cookies 已更新")
        else:
            yield event.plain_result("Instagram cookies 更新失败")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @instagram_manager.command('trigger_cache_update', alias={'手动缓存更新'})
    async def instagram_trigger_cache_update(self, event: AstrMessageEvent):
        if not self.ins_loader:
            yield event.plain_result("Instagram 功能未启用或登录失败")
            return
        ins_config = self.config.get('instagram_subscription_config', {})
        subscriptions = self.data_manager.get_instagram_subscriptions()
        logger.info("[Fiscok's][instagram] 手动触发 Instagram 缓存更新")
        for username in subscriptions:
            await asyncio.sleep(5)
            await fetch_instagram_posts(self.ins_loader, username, self.data_manager)
            if ins_config.get('instagram_fetch_stories', True):
                await asyncio.sleep(5)
                await fetch_instagram_stories(self.ins_loader, username, self.data_manager)
        yield event.plain_result("已手动触发 Instagram 缓存更新，请检查日志以验证更新过程")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @instagram_manager.command('trigger_push', alias={'手动推送'})
    async def instagram_trigger_push(self, event: AstrMessageEvent):
        await self.instagram_scheduled_push()
        yield event.plain_result("已手动触发 Instagram 推送，请检查对应群聊以验证推送内容")

    # --- Cookie 管理辅助方法 ---
    def _load_cookies(self) -> Dict:
        """加载统一 cookie 文件"""
        if not self.cookies_path.exists():
            # 创建默认模板
            default = {"twitter": {"auth_token": "", "ct0": ""}, "instagram": {}}
            self._save_cookies(default)
            return default
        try:
            with open(self.cookies_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Fiscok's] 加载 cookies.json 失败: {e}")
            return {"twitter": {}, "instagram": {}}

    def _save_cookies(self, cookies: Dict = None):
        """保存统一 cookie 文件"""
        if cookies is None:
            cookies = self.cookies
        try:
            with open(self.cookies_path, 'w', encoding='utf-8') as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Fiscok's] 保存 cookies.json 失败: {e}")

    # --- 插件销毁方法 ---
    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.running = False
        self.timer.shutdown()

        # 取消所有后台任务并释放辅助 Provider
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self._topic_sessions.clear()
        self._group_recent.clear()
        self._proactive_states.clear()
        self._persona_recent.clear()
        await close_aux_providers()

        self.data_manager = None

        logger.info(f"{self.name} 插件已被卸载/停用，相关资源已清理")

    # --- 辅助方法 ---
    def _quote_info_create(self, alias: str, account_id: str,
                           cache_getter, platform_name: str = "动态",
                           text_fallback: bool = False) -> Nodes | None:
        """
        通用的转发消息构建方法（推特和 Instagram 共用）

        Args:
            alias: 显示别名
            account_id: 账号 ID（twitter_id 或 username）
            cache_getter: 获取缓存的方法（如 data_manager.get_twitter_cache）
            platform_name: 平台名称（用于标题文案）
            text_fallback: 文本为空时是否使用 content_type 作为兜底
        """
        def _create_node(_text: str, _image_urls: List[str]) -> Node:
            content_list: List[Any] = [Plain(_text)]
            for _url in _image_urls:
                if _url:
                    content_list.append(Image.fromFileSystem(_url))
            return Node(
                uin=640439951,
                name="鱼豆腐转发版",
                content=content_list
            )

        caches = cache_getter(account_id)[:10]
        if not caches:
            return None

        nodes = [
            Node(
                uin=640439951,
                name="鱼豆腐转发版",
                content=[Plain(f"{alias} @{account_id} 的最新{platform_name}，共 {len(caches)} 条")]
            )
        ]

        for cache in caches:
            text = cache.get('text', '')
            if not text and text_fallback:
                content_type = cache.get('content_type', 'post')
                text = f"[{content_type}]"
            nodes.append(_create_node(text, cache.get('images', [])))

        return Nodes(nodes=nodes)
