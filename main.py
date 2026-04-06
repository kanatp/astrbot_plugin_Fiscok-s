from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.message_components import Node, Plain, Image

from .core.api.bili_apis import get_bvid
from .core.api.storage_apis import DataManager
from .core.net.twitter_fetch import fetch_twitter_data, check_availability

import subprocess
import asyncio
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing import List, Dict, Any

@register("Fiscok-s Plugins", "Fiscok", "Fiscok自用插件", "1.0")
class Core(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.running = True

        self.config = config
        self.plugin_data_path = get_astrbot_data_path() + "/plugin_data/" + self.name
        self.data_manager = DataManager(self.plugin_data_path, config)

        # 添加缓存轮询更新任务
        asyncio.create_task(self.twitter_cache_update())

        # 添加定时推送推特内容任务
        self.timer = AsyncIOScheduler()
        time_list = self.config.get("twitter_push_time", [])
        for time_str in time_list:
            self.timer.add_job(
                self.twitter_scheduled_push,
                'cron',
                hour=int(time_str.split(":")[0]),
                minute=int(time_str.split(":")[1])
            )

        # 启动定时任务
        self.timer.start()

    @filter.on_llm_request()
    async def handle_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在获取 LLM 回复之前拦截请求，并注入提示词修改
        """
        logger.info(f"被拦截的请求体如下: {req.system_prompt}")
        ## 循环原始消息获取其中所有的表情包
        for message_part in event.message_obj.raw_message.message:
            if message_part.get("type") == "image" and message_part.get("data", {}).get("sub_type") == 1:
                logger.info(f"找到表情包: {message_part.get('data', {}).get('url')}")

    # 临时测试用指令
    @filter.command('test', alias={'测试'})
    async def test_command(self, event: AstrMessageEvent):
        """
        这是一个测试指令，用于验证推特缓存功能
        """
        await fetch_twitter_data('aimi_sound', self.data_manager)
        yield event.plain_result("已执行测试指令，检查日志以验证推特")

    # --- Bilibili视频发布统计（火星救援） ---
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bili_video_count(self, event: AstrMessageEvent):
        """
        解析 Bilibili 链接并判断是否在该群聊被发送过
        """
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
            await asyncio.sleep(3600)  # 每小时更新一次
            if self.config.get("twitter_subscription_available"):
                subscriptions = self.data_manager.get_twitter_subscriptions()
                for twitter_id in subscriptions:
                    await asyncio.sleep(180) # 每次请求间隔3分钟，避免过于频繁导致推特账号异常
                    await fetch_twitter_data(twitter_id, self.data_manager)

    # --- 推特定时推送 ---
    async def twitter_scheduled_push(self):
        """
        将未推送的推特缓存推送到对应的群聊
        """
        subscriptions = self.data_manager.get_all_twitter_subscriptions()

        for subscription in subscriptions:
            alias = subscription['alias'] if subscription['alias'] else subscription['twitter_id']
            twitter_id = subscription['twitter_id']

            title_node = {
                "type": "Node",
                "uin": "640439951",
                "name": "鱼豆腐转发版",
                "content": [
                    {"type": "Plain", "text": f"{alias} 的推特账号 @{twitter_id} 的最新动态"}
                ]
            }
            body_node = self._quote_info_create_api(subscription['twitter_id'])
            message_payload = [title_node, body_node]

            groups: List[str] = subscription['groups']
            for group in groups:
                # umo 格式：platform:message_type:session_id
                umo = f"aiocqhttp:GroupMessage:{group}"
                await self._send_proactive_message(
                    umo,
                    message_payload
                )

    # --- HTTP API 主动推送消息 ---
    async def _send_proactive_message(self, umo: str, messages: list):
        """通过 AstrBot HTTP API 主动推送消息"""
        url = f"http://localhost:{self.config.get("port")}/api/v1/im/message"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.config.get("api_key", "YOUR_SECRET_TOKEN")
        }
        payload = {
            "umo": umo,
            "message": messages
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    print(f"推送失败: {umo}, status={resp.status}")

    # --- 推特订阅推送指令组 ---
    @filter.command_group('twitter_manager', alias={'推特管理'})
    def twitter_manager(self):
        pass

    @twitter_manager.command('subscribe', alias={'订阅'})
    async def twitter_subscribe(self, event: AstrMessageEvent, twitter_id: str, alias: str = None):
       self.data_manager.add_twitter_subscription(twitter_id, event.get_group_id(), alias)
       yield event.plain_result(f"已订阅推特账号 @{twitter_id}，别名: {alias if alias else '无'}，请等待更新推送")

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
            response_message += f"- @{sub['twitter_id']} (别名: {sub['alias'] if sub['alias'] else '无'})\n"
        yield event.plain_result(response_message)

    @twitter_manager.command('update_cookie', alias={'更新Cookie'})
    async def twitter_update_cookie(self, event: AstrMessageEvent, auth_token: str, ct0: str):
        env_path = "/rsshub/.env"  # 容器内的挂载路径

        with open(env_path, "w") as f:
            f.write(f"TWITTER_AUTH_TOKEN={auth_token}\n")
            f.write(f"TWITTER_CT0={ct0}\n")

        subprocess.run(["docker", "restart", "rsshub"])
        logger.info("已更新 Twitter Cookie 并重启 RSSHub，新的订阅推送将在几分钟内生效")
        yield event.plain_result("已更新 Twitter Cookie 并重启 RSSHub，新的订阅推送将在几分钟内生效")

    @twitter_manager.command('check_available', alias={'检查连接状态'})
    async def twitter_check_available(self, event: AstrMessageEvent):
        status = await check_availability()
        if status:
            yield event.plain_result("RSSHub 服务连接正常，可以正常获取推特更新")
        else:
            yield event.plain_result("RSSHub 服务连接异常，请检查服务状态和网络连接")

    # --- 图库管理指令组 ---
    @filter.command_group('gallery_manager', alias={'图库管理'})
    def gallery_manager(self):
        pass

    # --- 插件销毁方法 ---
    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.running = False
        self.timer.shutdown()
        self.data_manager = None

        logger.info(f"{self.name} 插件已被卸载/停用，相关资源已清理")

    # --- 辅助方法 ---
    def _quote_info_create_api(self, twitter_id: str) -> dict:
        """构建适用于 HTTP API 的合并转发消息结构（dict 格式）"""

        def _create_node(_text: str, _image_urls: List[str]) -> dict:
            content_list: List[dict] = [
                {"type": "Plain", "text": _text}
            ]
            for url in _image_urls:
                content_list.append({
                    "type": "Image",
                    "file": url,  # 本地路径对应 file 字段
                    "url": ""
                })
            return {
                "type": "Node",
                "uin": "640439951",
                "name": "鱼豆腐转发版",
                "content": content_list
            }

        caches = self.data_manager.get_twitter_cache(twitter_id)
        merge_content: List[dict] = []

        for cache in caches:
            text = cache['text']
            image_urls = cache['images']
            node = _create_node(text, image_urls)
            merge_content.append(node)

        # 外层包裹 Node，content 是内层 node list
        final_node = {
            "type": "Node",
            "uin": "640439951",
            "name": "鱼豆腐转发版",
            "content": merge_content
        }

        return final_node
