from pathlib import Path
import json
from datetime import datetime
from typing import Dict, List
import re
import asyncio

from astrbot.api import logger, AstrBotConfig

class DataManager:
    """
    用于基本的文件管理
    """
    def __init__(self, root: str, config: AstrBotConfig):
        self.root = Path(root)
        self.bili_video_root = self.root / 'bili_videos'
        self.twitter_cache_root = self.root / 'twitter_cache'

        self.config = config

        if not self.root.exists():
            self.create_folder(self)

    @staticmethod
    def create_folder(self):
        """
        初始化文件目录
        """
        if not self.root.exists():
            logger.info('[DataManager] 检测到数据为空，正在初始化......]')

            self.root.mkdir(parents=True, exist_ok=True)
            self.bili_video_root.mkdir(parents=True, exist_ok=True)
            self.twitter_push_root.mkdir(parents=True, exist_ok=True)

            logger.info('[DataManager] 已完成数据目录构建]')
        else:
            logger.info('[DataManager] 数据目录已存在')

    # --- bilibili视频统计基础组件 ---
    def _get_group_file(self, group_id: str) -> Path:
        """获取群组对应的 JSON 文件路径"""
        return self.bili_video_root / f'{group_id}.json'

    def _load_group_data(self, group_id: str) -> List:
        """读取群组 JSON 数据，文件不存在则返回空列表"""
        file = self._get_group_file(group_id)
        if not file.exists():
            return []
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_group_data(self, group_id: str, data: List):
        """将数据写回群组 JSON 文件"""
        file = self._get_group_file(group_id)
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # -- bilibili视频统计核心接口 ---
    def get_bili_video_storage(self, group_id: str, bvid: str) -> Dict | None:
        """
        查询某群组中某视频的分享记录

        :param group_id: 群组 ID
        :param bvid: 视频 BV 号
        :return: 存在时返回 {'first_sharer': ..., 'timestamp': ..., 'count': ...}，否则返回 None
        """
        data = self._load_group_data(group_id)
        query_result = None
        for entry in data:
            if entry.get('bvid') == bvid:
                entry.update({'count': entry.get('count') + 1})
                query_result = {
                    'first_sharer': entry['first_sharer'],
                    'timestamp': entry['timestamp'],
                    'count': entry['count'],
                }
        self._save_group_data(group_id, data)
        return query_result

    def update_bili_video_storage(self, group_id: str, sender_nickname: str,  sender_id: str, bvid: str):
        """
        新增某群组中某视频的分享记录（仅在不存在时写入）

        :param group_id: 群组 ID
        :param sender_nickname: 发送者昵称
        :param sender_id: 发送者 ID（作为 first_sharer）
        :param bvid: 视频 BV 号
        """
        data = self._load_group_data(group_id)

        for entry in data:
            if entry.get('bvid') == bvid:
                logger.warning(f'[DataManager] bvid={bvid} 在群 {group_id} 中已存在，跳过写入')
                return

        new_entry = {
            'bvid': bvid,
            'first_sharer': f'{sender_nickname}（{sender_id}）',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'count': 1,
        }
        data.append(new_entry)
        self._save_group_data(group_id, data)
        logger.info(f'[DataManager] 已记录 bvid={bvid} 由 {sender_id} 首次分享于群 {group_id}')

    # --- 推特数据缓存基础组件 ---
    @staticmethod
    def _get_image_extension(url: str, content_type: str = "") -> str:
        """从 URL 参数或 Content-Type 推断图片扩展名"""
        m = re.search(r"[?&]format=(\w+)", url)
        if m:
            return f".{m.group(1)}"
        path_ext = url.split("?")[0].rsplit(".", 1)
        if len(path_ext) == 2 and len(path_ext[1]) <= 5:
            return f".{path_ext[1]}"
        ct_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        for ct, ext in ct_map.items():
            if ct in content_type:
                return ext
        return ".jpg"

    async def _twitter_image_download(self, twitter_id: str, content_id: str, image_urls: List) -> List[Path]:
        """
        下载推特图片并返回本地路径
        """
        import aiohttp
        import aiofiles

        async def download_image(
                _session: aiohttp.ClientSession,
                _url: str,
                _save_dir: Path
        ):
            """下载单张图片"""
            async with _session.get(_url) as response:
                response.raise_for_status()
                async with aiofiles.open(_save_dir, mode="wb") as f:
                    async for chunk in response.content.iter_chunked(1024):
                        await f.write(chunk)

        """并发下载所有图片"""
        save_dir = self.twitter_cache_root / twitter_id / content_id
        save_dir.mkdir(parents=True, exist_ok=True)

        local_urls = []

        async with aiohttp.ClientSession() as session:
            # 用 for 循环构建任务列表
            tasks = []
            for url in image_urls:
                filename = f"{content_id}_{len(tasks)}{self._get_image_extension(url)}"
                task = asyncio.create_task(
                    download_image(
                        session,
                        url,
                        save_dir / filename
                    )
                )
                local_urls.append(save_dir / filename)
                tasks.append(task)

            # 并发执行所有任务，gather 会等待全部完成
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"[Fiscok's][dataManager][twitterImageDownload]下载图片失败: {result}")
                    local_urls[idx] = None  # 标记下载失败的图片路径为 None
            logger.info(f"[Fiscok's][dataManager][twitterImageDownload]下载完成，结果: {results}")

        return local_urls

    def _get_push_record(self) -> Path:
        """获取推特内容对应的 JSON 文件路径"""
        return self.twitter_cache_root / "push_record.json"

    def _load_push_record(self) -> Dict:
        """读取推特内容推送记录，文件不存在则返回空字典"""
        file = self._get_push_record()
        if not file.exists():
            return {}
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_push_record(self, record: Dict):
        """将推特内容推送记录写回 JSON 文件"""
        file = self._get_push_record()
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    def _get_cache_list(self, twitter_id: str) -> Path:
        """获取指定 twitter_id 的缓存列表，按照 content_id 排序"""
        return self.twitter_cache_root / f"{twitter_id}.json"

    def _load_cache_list(self, twitter_id: str) -> List[Dict]:
        """读取指定 twitter_id 的缓存列表，返回按照 content_id 排序的列表"""
        dir_path = self._get_cache_list(twitter_id)
        result = []
        if dir_path.exists():
            with open(dir_path, 'r', encoding='utf-8') as f:
                try:
                    result = json.load(f)
                except json.JSONDecodeError:
                    logger.error(f"[Fiscok's][dataManager][_load_cache_list]解析缓存列表失败，文件可能损坏: {dir_path}")
        return result

    def _save_cache_list(self, twitter_id: str, cache_list: List[Dict]):
        """将指定 twitter_id 的缓存列表写回 JSON 文件"""
        dir_path = self._get_cache_list(twitter_id)
        with open(dir_path, 'w', encoding='utf-8') as f:
            json.dump(cache_list, f, ensure_ascii=False, indent=2)

    # --- 推特数据缓存核心接口 ---
    def get_twitter_cache(self, twitter_id: str) -> List[Dict]:
        """
        返回指定推特账号的未被推送的缓存数据列表，供定时推送使用
        """
        cache_list = self._load_cache_list(twitter_id)
        push_record = self._load_push_record()

        result = []
        for item in cache_list:
            content_id = item.get("content_id")
            if content_id and content_id not in push_record.get("record_list", []):
                content_path = self.twitter_cache_root / twitter_id / content_id / "content.txt"
                if content_path.exists():
                    with open(content_path, 'r', encoding='utf-8') as f:
                        try:
                            content_data = json.load(f)
                            result.append(content_data)
                        except json.JSONDecodeError:
                            logger.error(f"[Fiscok's][dataManager][getTwitterCache]解析缓存内容失败，文件可能损坏: {content_path}")
                else:
                    logger.warning(f"[Fiscok's][dataManager][getTwitterCache]缓存内容文件不存在: {content_path}")

                push_record.setdefault("record_list", []).append(content_id)

        push_record["last_push"] = datetime.now().isoformat()
        self._save_push_record(push_record)
        return result

    def update_twitter_cache(self, update_content: Dict):
        """
        缓存推特更新内容，供定时推送使用，最大缓存数量由config给出，超出后按照时间戳淘汰最旧的记录
        """
        # 基于当前内容构建目录和文件路径
        twitter_id = update_content.get("twitter_id")
        content_id = update_content.get("content_id")
        if not twitter_id or not content_id:
            logger.warning(
                f"[Fiscok's][dataManager][updateTwitterCache]缺少 twitter_id 或 content_id，无法缓存: {update_content}"
            )
            return

        dir_path = self.twitter_cache_root / twitter_id / content_id
        dir_path.mkdir(parents=True, exist_ok=True)

        # 缓存图片
        image_urls = update_content.get("images", [])
        local_image_paths = []
        if not image_urls:
            logger.info(f"[Fiscok's][dataManager][updateTwitterCache]没有图片需要下载，跳过图片缓存")

        local_image_paths = asyncio.run(
            self._twitter_image_download(twitter_id, content_id, image_urls)
        )
        logger.info(f"[Fiscok's][dataManager][updateTwitterCache]图片下载完成，local_image_paths={local_image_paths}")

        # 将文本内容写入一个文本文件，方便后续推送时读取
        output_file = {
            "twitter_id": twitter_id,
            "content_id": content_id,
            "text": update_content.get("text", ""),
            "images": local_image_paths,
            "timestamp": update_content.get("timestamp"),
        }
        text_path = dir_path / "content.txt"

        with open(text_path, 'w', encoding='utf-8') as f:
            json.dump(output_file, f, ensure_ascii=False, indent=2)

        logger.info(
            f"[Fiscok's][dataManager][updateTwitterCache]已缓存内容: "
            f"twitter_id={twitter_id}, "
            f"content_id={content_id}, "
            f"images={len(image_urls)} "
        )

        cache_list = self._load_cache_list(twitter_id)
        cache_list.append({
            "content_id": content_id,
            "timestamp": update_content.get("timestamp"),
        })

        # 统计当前 twitter_id 的缓存数量，超过限制则删除时间戳最早的记录
        max_cache = self.config.get("twitter_cache_max", 100)
        if len(cache_list) > max_cache:
            cache_list.sort(key=lambda x: x.get("timestamp", ""))
            to_remove = cache_list[:-max_cache]
            for item in to_remove:
                remove_path = self.twitter_cache_root / twitter_id / item["content_id"]
                if remove_path.exists():
                    for child in remove_path.iterdir():
                        child.unlink()
                    remove_path.rmdir()
                    logger.info(f"[Fiscok's][dataManager][updateTwitterCache]已淘汰过期缓存: {remove_path}")
            cache_list = cache_list[-max_cache:]
        self._save_cache_list(twitter_id, cache_list)

    def cache_in_storage(self, twitter_id: str, content_id: str) -> bool:
        """检查指定 content_id 的推特内容是否已存在缓存中，避免重复解析入库"""
        return (self.twitter_cache_root / twitter_id / content_id).exists()

    def cache_been_pushed(self, group_id: str, twitter_id: str) -> bool:
        """检查指定 twitter_id 的内容是否已推送到 group_id 中，避免重复推送"""
        record = self._load_push_record()
        return twitter_id in record.get(group_id, [])

    # --- 推特订阅管理 ---
    def add_twitter_subscription(self, group_id: str, twitter_id: str):
        """
        添加推特订阅记录
        """
        pass

    def remove_twitter_subscription(self, group_id: str, twitter_id: str):
        """
        移除推特订阅记录
        """
        pass

    def update_twitter_target_groups(self, twitter_id: str, group_id: str):
        """
        更新推特订阅的目标群聊列表

        :param twitter_id: 推特账号
        :param group_id: 群聊 ID
        """
        pass

    def get_twitter_target_groups(self, twitter_id: str) -> list:
        """
        获取订阅某推特账号的目标群聊列表

        :param twitter_id: 推特账号
        :return: 群聊 ID 列表
        """
        pass


