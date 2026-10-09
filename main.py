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
from .core.prompts import format_meme_placeholder_injection, format_emotion_state_injection
from .core.net.twitter_fetch import fetch_twitter_data, check_availability
from .core.net.instagram_fetch import create_loader, fetch_instagram_posts, fetch_instagram_stories, check_instagram_access

import subprocess
import asyncio
import random
import aiohttp
import aiofiles
import json
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing import List, Dict, Any
import re

@register("Fiscok-s Plugins", "Fiscok", "Fiscok自用插件", "1.0")
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
                provider_id = meme_config.get('llm_provider_id', '')
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
