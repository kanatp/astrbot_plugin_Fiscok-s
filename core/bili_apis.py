'''
用于处理 Bilibili 链接并返回 BV 号
'''
import re
import json
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

import aiohttp

async def get_bvid(event: AstrMessageEvent) -> str:
    """识别视频链接返回 BV 号"""
    raw_msg = event.message_str or ""
    bili_url = ""
    bvid = None

    # ---- 1. 尝试从 raw_message 提取 QQ 小程序 / JSON 卡片 ----
    try:
        if hasattr(event, 'message_obj') and event.message_obj:
            raw = getattr(event.message_obj, 'raw_message', None)
            if raw:
                bili_url = _extract_bili_url_from_raw(raw)

            # 遍历消息组件的 raw / data 属性
            if not bili_url and event.message_obj.message:
                for comp in event.message_obj.message:
                    comp_raw = getattr(comp, 'raw', None) or getattr(comp, 'data', None)
                    if comp_raw:
                        bili_url = _extract_bili_url_from_raw(comp_raw)
                        if bili_url:
                            break

            # 兜底：尝试将每个组件转为字符串后解析 JSON
            if not bili_url and event.message_obj.message:
                for comp in event.message_obj.message:
                    comp_str = str(comp)
                    if 'bilibili' in comp_str.lower() or 'b23.tv' in comp_str.lower():
                        # 尝试直接 JSON 解析
                        bili_url = _try_parse_json_for_url(comp_str)
                        if bili_url:
                            break
                        # 尝试从字符串中直接用正则匹配 URL
                        url_match = re.search(r'https?://[^\s\"\'\}\]]+bilibili\.com/video/(BV[0-9A-Za-z]{10})',
                                              comp_str)
                        if url_match:
                            bvid = url_match.group(1)
                            break
                        url_match = re.search(r'https?://b23\.tv/\S+', comp_str)
                        if url_match:
                            bili_url = url_match.group(0).rstrip('"}\']')
                            break
                        # qqdocurl 可能直接在字符串中
                        qqdoc_match = re.search(r'"qqdocurl"\s*:\s*"(https?://[^"]+)"', comp_str)
                        if qqdoc_match:
                            bili_url = qqdoc_match.group(1)
                            break
    except Exception as e:
        logger.error(f"[AutoDetect] 解析消息异常: {e}", exc_info=True)

    # ---- 2. message_str 可能本身就是 JSON ----
    if not bili_url and not bvid and raw_msg.strip().startswith("{"):
        bili_url = _try_parse_json_for_url(raw_msg.strip())

    # ---- 3. 如果从 JSON 拿到了 URL，提取 BV 号 ----
    if bili_url:
        bv_match = re.search(r'(BV[0-9A-Za-z]{10})', bili_url)
        if bv_match:
            bvid = bv_match.group(1)
        elif 'b23.tv' in bili_url or 'bili' in bili_url:
            resolved = await resolve_short_url(bili_url)
            if resolved:
                bv_match = re.search(r'(BV[0-9A-Za-z]{10})', resolved)
                if bv_match:
                    bvid = bv_match.group(1)

    # ---- 4. 从纯文本中提取 ----
    if not bvid:
        all_text = raw_msg
        try:
            if hasattr(event, 'message_obj') and event.message_obj:
                parts = []
                for comp in (event.message_obj.message or []):
                    if hasattr(comp, 'text'):
                        parts.append(comp.text)
                    elif isinstance(comp, str):
                        parts.append(comp)
                if parts:
                    all_text = " ".join(parts)
        except Exception:
            pass

        # BV 号
        bv_match = re.search(r'(BV[0-9A-Za-z]{10})', all_text)
        if bv_match:
            bvid = bv_match.group(1)

        # bilibili.com 长链
        if not bvid:
            url_match = re.search(r'https?://(?:www\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10})', all_text)
            if url_match:
                bvid = url_match.group(1)

        # b23.tv 短链 (异步解析)
        if not bvid:
            short_match = re.search(r'https?://b23\.tv/\S+', all_text)
            if short_match:
                resolved = await resolve_short_url(short_match.group(0))
                if resolved:
                    bv_match = re.search(r'(BV[0-9A-Za-z]{10})', resolved)
                    if bv_match:
                        bvid = bv_match.group(1)

    if not bvid:
        return '' # 没有检测到B站链接，静默放过

    return bvid


# ---- 小程序 URL 提取辅助方法 ----

def _extract_bili_url_from_raw(raw) -> str:
    """从 raw_message 中提取 B站 URL，支持 dict/list/str 格式"""
    if raw is None:
        return ""

    # raw 是 dict（已解析的 JSON 或 OneBot 消息段）
    if isinstance(raw, dict):
        url = _find_bili_qqdocurl(raw)
        if url:
            return url
        # OneBot 消息段: {"type":"json","data":{"data":"{...}"}}
        if raw.get("type") == "json":
            inner = raw.get("data", {})
            if isinstance(inner, dict):
                json_str = inner.get("data", "")
                if isinstance(json_str, str):
                    return _try_parse_json_for_url(json_str)
            elif isinstance(inner, str):
                return _try_parse_json_for_url(inner)

    # raw 是 list（OneBot 消息段列表）
    if isinstance(raw, list):
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "json":
                inner = seg.get("data", {})
                if isinstance(inner, dict):
                    json_str = inner.get("data", "")
                    if isinstance(json_str, str):
                        url = _try_parse_json_for_url(json_str)
                        if url:
                            return url
                elif isinstance(inner, str):
                    url = _try_parse_json_for_url(inner)
                    if url:
                        return url

    # raw 是 str
    if isinstance(raw, str):
        raw_str = raw.strip()
        # 纯 JSON 字符串
        if raw_str.startswith("{"):
            url = _try_parse_json_for_url(raw_str)
            if url:
                return url
        # CQ 码: [CQ:json,data=...]
        cq_match = re.search(r'\[CQ:json,data=(.*?)\]', raw_str, re.S)
        if cq_match:
            cq_data = cq_match.group(1)
            cq_data = (
                cq_data
                .replace("&amp;", "&")
                .replace("&#44;", ",")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
            )
            url = _try_parse_json_for_url(cq_data)
            if url:
                return url

    return ""


def _try_parse_json_for_url(text: str) -> str:
    """尝试从 JSON 字符串中提取 B站 URL"""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _find_bili_qqdocurl(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _find_bili_qqdocurl(data: dict) -> str:
    """从已解析的 JSON dict 中查找 B站相关的 URL"""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return ""
    for _key, val in meta.items():
        if isinstance(val, dict):
            url = val.get("qqdocurl", "") or val.get("jumpUrl", "") or val.get("url", "")
            if url and _is_bili_domain(url):
                return url
    return ""


def _is_bili_domain(url: str) -> bool:
    """检查 URL 是否属于 B站 相关域名"""
    import urllib.parse
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        host = host.lower().rstrip(".")
        bili_domains = ("bilibili.com", "b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn", "bili33.cn")
        return any(host == d or host.endswith("." + d) for d in bili_domains)
    except Exception:
        return False

# ---- 解析工具 ----

timeout = aiohttp.ClientTimeout(total=10)
async def resolve_short_url(short_url: str) -> Optional[str]:
    """
    异步解析短链接（如 b23.tv），返回跳转后的最终 URL
    """
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(short_url, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }) as resp:
                return str(resp.url)
    except Exception as e:
        logger.warning(f"解析短链接失败: {e}")
        return None