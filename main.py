from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.bili_apis import get_bvid
from .core.storage_apis import DataManager

@register("Fiscok's Plugins", "Fiscok", "Fiscok自用插件", "1.0")
class Core(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.plugin_data_path = get_astrbot_data_path() + "plugin_data/" + self.name
        self.data_manager = DataManager(self.plugin_data_path)

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

                response_message = (f'本消息已经被{first_sharer}于{timestamp}发布过啦！'
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

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
