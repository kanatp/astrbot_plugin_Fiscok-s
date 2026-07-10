from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.message_components import Node, Plain, Image, Nodes, Reply, Forward

from .core.api.bili_apis import get_bvid
from .core.api.storage_apis import DataManager
from .core.api.meme_apis import generate_meme_description
from .core.prompts import format_meme_placeholder_injection
from .core.net.twitter_fetch import fetch_twitter_data, check_availability
from .core.net.instagram_fetch import create_loader, fetch_instagram_posts, fetch_instagram_stories, check_instagram_login

import subprocess
import asyncio
import random
import aiohttp
import aiofiles
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

        # --- Instagram 订阅初始化 ---
        self.ins_loader = None
        ins_config = self.config.get('instagram_subscription_config', {})
        if ins_config.get('instagram_subscription_available', False):
            ins_username = ins_config.get('instagram_username', '')
            ins_password = ins_config.get('instagram_password', '')
            self.ins_loader = create_loader(ins_username, ins_password)
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
                logger.warning("[Fiscok's][instagram] Instagram 登录失败，订阅功能未启用")

        # 启动定时任务
        self.timer.start()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def meme_learn_on_message(self, event: AstrMessageEvent):
        """
        在每次收到消息时触发表情包偷取判定（与 LLM 请求解耦）
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

        if random.random() < learn_probability:
            await self._learn_meme_from_message(event, meme_config)

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

    async def _learn_meme_from_message(self, event: AstrMessageEvent, meme_config: Dict):
        """
        从消息中学习表情包：检测、下载、调用 LLM 生成描述、入库
        """
        try:
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
                return

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

                if not is_emoji or not image_url:
                    continue

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
                source = f"group_{event.get_group_id()}" if event.get_group_id() else "private"
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
            if twitter_config.get("twitter_subscription_available"):
                subscriptions = self.data_manager.get_twitter_subscriptions()
                logger.info(f"[Fiscok's][twitter_push]正在更新推特缓存")
                for twitter_id in subscriptions:
                    logger.info(f"[Fiscok's][twitter_push]正在拉取推特账号 @{twitter_id} 的最新动态")
                    await asyncio.sleep(180) # 每次请求间隔3分钟，避免过于频繁导致推特账号异常
                    await fetch_twitter_data(twitter_id, self.data_manager, self.rssHub_full_url)

    # --- 推特定时推送 ---
    async def twitter_scheduled_push(self):
        """
        将未推送的推特缓存推送到对应的群聊
        """
        logger.info(f"[Fiscok's][twitter_push]正在执行定时推送任务")
        subscriptions = self.data_manager.get_all_twitter_subscriptions()
        unified_msg_origins = self.data_manager.get_umo()
        logger.info(f"[Fiscok's][twitter_push]当前订阅列表: {subscriptions}")

        for subscription in subscriptions:
            logger.info(f"[Fiscok's][twitter_push]正在处理订阅 @{subscription['twitter_id']} 的推送")
            alias = subscription['alias'] if subscription['alias'] else subscription['twitter_id']
            twitter_id = subscription['twitter_id']
            group_ids = subscription['group_ids']

            forward_node = self._quote_info_create(
                alias=alias,
                account_id=twitter_id,
                cache_getter=self.data_manager.get_twitter_cache,
                platform_name="动态"
            )
            if forward_node is None:
                logger.info(f"[Fiscok's][twitter_push]未找到 @{twitter_id} 的有效缓存，跳过推送")
                continue
            message_chain = MessageChain(chain=[forward_node])

            for group_id in group_ids:
                umo = unified_msg_origins.get(group_id)
                logger.info(f"[Fiscok's][twitter_push]正在向群 {group_id} 推送 @{twitter_id} 的最新动态")
                await asyncio.sleep(20)  # 每次推送间隔20秒，避免过于频繁导致消息发送失败
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
        logger.info(f"{self.config}")
        if self.config.get('twitter_subscription_config', {}).get("twitter_subscription_available"):
            subscriptions = self.data_manager.get_twitter_subscriptions()
            logger.info(f"[Fiscok's][twitter_push]正在更新推特缓存")
            for twitter_id in subscriptions:
                logger.info(f"[Fiscok's][twitter_push]正在拉取推特账号 @{twitter_id} 的最新动态")
                await asyncio.sleep(180)  # 每次请求间隔3分钟，避免过于频繁导致推特账号异常
                await fetch_twitter_data(twitter_id, self.data_manager, self.rssHub_full_url)
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

            # 检查登录状态
            if not await check_instagram_login(self.ins_loader):
                logger.warning("[Fiscok's][instagram] Instagram 登录已失效，尝试重新登录")
                ins_username = ins_config.get('instagram_username', '')
                ins_password = ins_config.get('instagram_password', '')
                self.ins_loader = create_loader(ins_username, ins_password)
                if not self.ins_loader or not await check_instagram_login(self.ins_loader):
                    logger.error("[Fiscok's][instagram] Instagram 重新登录失败，跳过本次更新")
                    continue
                logger.info("[Fiscok's][instagram] Instagram 重新登录成功")

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
    @instagram_manager.command('check_login', alias={'检查登录状态'})
    async def instagram_check_login(self, event: AstrMessageEvent):
        if not self.ins_loader:
            yield event.plain_result("Instagram 功能未启用或登录失败，请先使用 ins管理 login 重新登录")
            return
        status = await check_instagram_login(self.ins_loader)
        if status:
            yield event.plain_result("Instagram 登录状态正常")
        else:
            yield event.plain_result("Instagram 未登录或登录已过期，请使用 ins管理 login 重新登录")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @instagram_manager.command('login', alias={'登录'})
    async def instagram_login(self, event: AstrMessageEvent):
        ins_config = self.config.get('instagram_subscription_config', {})
        ins_username = ins_config.get('instagram_username', '')
        ins_password = ins_config.get('instagram_password', '')
        if not ins_username:
            yield event.plain_result("未配置 Instagram 用户名，请在配置中填写 instagram_username")
            return
        self.ins_loader = create_loader(ins_username, ins_password)
        if self.ins_loader:
            yield event.plain_result(f"Instagram 登录成功: @{ins_username}")
        else:
            yield event.plain_result(
                f"Instagram 登录失败。如遇 Checkpoint 验证，请在本地执行 "
                f"`instaloader --login={ins_username}` 完成验证后，"
                f"将 session-{ins_username} 文件上传到服务器，再执行此命令重试。"
            )

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

    # --- 插件销毁方法 ---
    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.running = False
        self.timer.shutdown()
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
