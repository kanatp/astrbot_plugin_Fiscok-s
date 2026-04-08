from html.parser import HTMLParser
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

from astrbot.api import logger

from ..api.storage_apis import DataManager

# --- HTML 解析器（标准库） ---
class _DescriptionParser(HTMLParser):
    """
    单遍扫描 description HTML，完成：
    - 忽略 <div class="rsshub-quote">…</div> 内的所有内容
    - 收集正文 <img src="…"> 的 URL
    - 将 <br> 转为换行，提取纯文本
    """

    def __init__(self):
        super().__init__()
        self._div_depth = 0
        self._quote_start_depth = None  # quote div 开始时的 div 嵌套深度

        self.text_parts: list[str] = []
        self.image_urls: list[str] = []

    @staticmethod
    def _attr(attrs: list[tuple], name: str) -> str | None:
        for k, v in attrs:
            if k == name:
                return v
        return None

    def _in_quote(self) -> bool:
        return self._quote_start_depth is not None

    def handle_starttag(self, tag: str, attrs: list[tuple]):
        if tag == "div":
            self._div_depth += 1
            cls = self._attr(attrs, "class") or ""
            if "rsshub-quote" in cls.split() and not self._in_quote():
                self._quote_start_depth = self._div_depth
            return

        if self._in_quote():
            return  # quote 内部，全部忽略

        if tag == "br":
            self.text_parts.append("\n")
        elif tag == "img":
            src = self._attr(attrs, "src")
            if src:
                self.image_urls.append(src)

    def handle_endtag(self, tag: str):
        if tag == "div":
            if self._in_quote() and self._div_depth == self._quote_start_depth:
                self._quote_start_depth = None  # quote 结束
            self._div_depth -= 1

    def handle_data(self, data: str):
        if not self._in_quote():
            self.text_parts.append(data)

    def result(self) -> tuple[str, list[str]]:
        raw = "".join(self.text_parts)
        text = re.sub(r"\n{3,}", "\n\n", raw).strip()
        return text, self.image_urls


# --- 异步的目标抓取入库 ---
async def fetch_twitter_data(twitter_id: str, manager: DataManager, url: str):
    """
    :param twitter_id: 目标推特用户名（不带 @）
    :param manager: 数据管理器实例，用于访问存储
    :param url: rssHub接口地址，默认为本地部署地址
    此处从本地的rssHub中调用接口获取数据，传入推特用户名，返回并解析推特数据，最终返回一个包含推特信息的列表
    """
    import aiohttp

    # 上传之前记得修改！！
    if not url:
        logger.error("RSSHub URL 未配置，无法获取 Twitter 数据")
        return
    url = f"https://{url}/twitter/user/{twitter_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.exception(f"Failed to fetch Twitter data: {resp.status}")
                return
            xml_text = await resp.text()
            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is None:
                return

            for item in channel.findall("item"):
                desc_el = item.find("description")
                pubdate_el = item.find("pubDate")
                content_id_el = item.find("link")

                if desc_el is None or not (desc_el.text or "").strip():
                    continue

                content_id = content_id_el.text.split("/")[-1]
                if manager.cache_in_storage(twitter_id, content_id):
                    logger.info(f"[Fiscok's][twitter_fetch]内容 {content_id} 已存在缓存中，跳过")
                    continue  # 已缓存过，跳过
                if content_id == "":
                    logger.warning(f"[Fiscok's][twitter_fetch]未能正确解析 content_id，跳过")
                    continue

                text, image_urls = _extract_text_and_image_urls(desc_el.text or "")
                timestamp = _parse_pubdate(pubdate_el.text if pubdate_el is not None else "")

                formatted_context = {
                    "twitter_id": twitter_id,
                    "content_id": content_id,
                    "text": text,
                    "images": image_urls,
                    "timestamp": timestamp.isoformat() if timestamp else None,
                }

                await manager.update_twitter_cache(formatted_context)

# --- 工具函数 ---
def _parse_pubdate(date_str: str) -> datetime | None:
    """解析 RSS pubDate 字符串为带时区的 datetime"""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str.strip())
    except Exception:
        return None

def _extract_text_and_image_urls(raw_html: str) -> tuple[str, list[str]]:
    parser = _DescriptionParser()
    parser.feed(raw_html)
    return parser.result()

async def check_availability(url: str) -> bool:
    """
    检查 RSSHub 服务是否可用
    """
    import aiohttp
    test_url = f"https://{url}/twitter/user/aimi_sound"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"无法连接到 RSSHub 服务: {e}")
        return False