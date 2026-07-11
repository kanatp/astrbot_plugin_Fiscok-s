'''
Instagram 数据拉取模块
基于 instaloader 库，通过 cookies 访问帖子和快拍
'''
import asyncio
import json
from pathlib import Path
from typing import List, Dict, Optional

from astrbot.api import logger

from ..api.storage_apis import DataManager


def create_loader(cookies: Dict[str, str]) -> Optional[object]:
    """
    通过 cookies 创建 instaloader 实例

    Args:
        cookies: Instagram cookies 字典（{name: value} 格式）

    Returns:
        配置好的 Instaloader 实例，失败则返回 None
    """
    import instaloader

    if not cookies.get('sessionid'):
        logger.error("[Fiscok's][instagram_fetch] 缺少 sessionid cookie")
        return None

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )

    # 设置 cookies 到两个域名
    for name, value in cookies.items():
        if value:
            loader.context._session.cookies.set(name, value, domain='.instagram.com')
            loader.context._session.cookies.set(name, value, domain='www.instagram.com')

    logger.info("[Fiscok's][instagram_fetch] 已通过 cookies 初始化 instaloader")
    return loader


def _collect_posts(loader, username: str, manager: DataManager, max_posts: int = 5) -> List[Dict]:
    """
    同步拉取帖子数据（在线程中运行），返回待缓存的内容列表
    """
    import instaloader

    contents = []
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        logger.info(f"[Fiscok's][instagram_fetch] 正在拉取 @{username} 的帖子，粉丝数: {profile.followers}")

        # 调试：检查 profile 的内部数据结构
        logger.info(f"[Fiscok's][instagram_fetch] profile._node keys: {list(profile._node.keys()) if hasattr(profile, '_node') else 'N/A'}")
        if hasattr(profile, '_node') and 'edge_owner_to_timeline_media' in profile._node:
            media_data = profile._node['edge_owner_to_timeline_media']
            logger.info(f"[Fiscok's][instagram_fetch] media_data keys: {list(media_data.keys()) if isinstance(media_data, dict) else type(media_data)}")

        count = 0
        for post in profile.get_posts():
            if count >= max_posts:
                break

            shortcode = post.shortcode
            if manager.instagram_cache_in_storage(username, shortcode):
                logger.info(f"[Fiscok's][instagram_fetch] 帖子 {shortcode} 已存在缓存中，跳过")
                continue

            # 提取图片 URL
            image_urls = []
            if post.typename == "GraphSidecar":
                for node in post.get_sidecar_nodes():
                    if not node.is_video:
                        image_urls.append(node.display_url)
            elif post.typename == "GraphImage":
                image_urls.append(post.url)

            if not image_urls:
                logger.info(f"[Fiscok's][instagram_fetch] 帖子 {shortcode} 无图片，跳过")
                continue

            caption = post.caption or ""
            timestamp = post.date_utc.isoformat() if post.date_utc else None

            contents.append({
                "username": username,
                "shortcode": shortcode,
                "content_type": "post",
                "text": caption,
                "images": image_urls,
                "timestamp": timestamp,
                "likes": post.likes,
            })

            count += 1
            logger.info(f"[Fiscok's][instagram_fetch] 已收集帖子: {shortcode} ({count}/{max_posts})")

    except instaloader.exceptions.ProfileNotExistsException:
        logger.error(f"[Fiscok's][instagram_fetch] 用户 @{username} 不存在")
    except instaloader.exceptions.LoginRequiredException:
        logger.error(f"[Fiscok's][instagram_fetch] 拉取 @{username} 的帖子需要登录，请更新 cookies")
    except KeyError as e:
        logger.error(f"[Fiscok's][instagram_fetch] 拉取 @{username} 的帖子失败: KeyError {e}")
        # 尝试获取更多调试信息
        try:
            if hasattr(profile, '_node'):
                logger.error(f"[Fiscok's][instagram_fetch] profile._node 完整内容: {json.dumps(profile._node, ensure_ascii=False, indent=2)}")
        except Exception as debug_e:
            logger.error(f"[Fiscok's][instagram_fetch] 获取调试信息失败: {debug_e}")
    except Exception as e:
        logger.error(f"[Fiscok's][instagram_fetch] 拉取 @{username} 的帖子失败: {e}", exc_info=True)

    return contents


def _collect_stories(loader, username: str, manager: DataManager) -> List[Dict]:
    """
    同步拉取快拍数据（在线程中运行），返回待缓存的内容列表
    """
    import instaloader

    contents = []
    try:
        profile = instaloader.Profile.from_username(loader.context, username)

        stories = loader.get_stories(userids=[profile.userid])
        for story in stories:
            for item in story.get_items():
                shortcode = f"story_{item.mediaid}"

                if manager.instagram_cache_in_storage(username, shortcode):
                    continue

                # 快拍只提取图片（视频快拍暂不处理）
                image_urls = []
                if not item.is_video:
                    image_urls.append(item.url)

                if not image_urls:
                    continue

                timestamp = item.date_utc.isoformat() if item.date_utc else None

                contents.append({
                    "username": username,
                    "shortcode": shortcode,
                    "content_type": "story",
                    "text": "",
                    "images": image_urls,
                    "timestamp": timestamp,
                })

                logger.info(f"[Fiscok's][instagram_fetch] 已收集快拍: {shortcode}")

    except instaloader.exceptions.ProfileNotExistsException:
        logger.error(f"[Fiscok's][instagram_fetch] 用户 @{username} 不存在")
    except instaloader.exceptions.LoginRequiredException:
        logger.error(f"[Fiscok's][instagram_fetch] 拉取快拍需要登录，请更新 cookies")
    except Exception as e:
        logger.error(f"[Fiscok's][instagram_fetch] 拉取 @{username} 的快拍失败: {e}", exc_info=True)

    return contents


async def fetch_instagram_posts(loader, username: str, manager: DataManager, max_posts: int = 5):
    """
    拉取指定用户的最新帖子并缓存
    """
    contents = await asyncio.to_thread(_collect_posts, loader, username, manager, max_posts)
    for content in contents:
        await manager.update_instagram_cache(content)
        logger.info(f"[Fiscok's][instagram_fetch] 已缓存帖子: {content['shortcode']}")


async def fetch_instagram_stories(loader, username: str, manager: DataManager):
    """
    拉取指定用户的快拍（Stories）并缓存
    """
    contents = await asyncio.to_thread(_collect_stories, loader, username, manager)
    for content in contents:
        await manager.update_instagram_cache(content)
        logger.info(f"[Fiscok's][instagram_fetch] 已缓存快拍: {content['shortcode']}")


async def check_instagram_access(loader) -> bool:
    """
    检查 Instagram cookies 是否有效
    """
    import instaloader

    def _check():
        try:
            # 尝试访问一个公开页面来验证 cookies
            instaloader.Profile.from_username(loader.context, "instagram")
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_check)
