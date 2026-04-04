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
        self.plugin_data_path = get_astrbot_data_path() / "plugin_data" / self.name
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

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def bili_video_count(self, event: AstrMessageEvent):
        bvid = await get_bvid(event)
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if bvid and group_id and sender_id:
            pass
        else:
            # 默认通行
            return

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
