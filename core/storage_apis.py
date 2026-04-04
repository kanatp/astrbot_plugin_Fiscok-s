from pathlib import Path
import json
from datetime import datetime

from astrbot.api import logger

class DataManager:
    """
    用于基本的文件管理
    """
    def __init__(self, root: Path):
        self.root = root
        self.bili_video_root = self.root / 'bili_videos'

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

            logger.info('[DataManager] 已完成数据目录构建]')
        else:
            logger.info('[DataManager] 数据目录已存在')

    def _get_group_file(self, group_id: str) -> Path:
        """获取群组对应的 JSON 文件路径"""
        return self.bili_video_root / f'{group_id}.json'

    def _load_group_data(self, group_id: str) -> list:
        """读取群组 JSON 数据，文件不存在则返回空列表"""
        file = self._get_group_file(group_id)
        if not file.exists():
            return []
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_group_data(self, group_id: str, data: list):
        """将数据写回群组 JSON 文件"""
        file = self._get_group_file(group_id)
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_bili_video_storage(self, group_id: str, bvid: str) -> dict | None:
        """
        查询某群组中某视频的分享记录

        :param group_id: 群组 ID
        :param bvid: 视频 BV 号
        :return: 存在时返回 {'first_sharer': ..., 'timestamp': ..., 'count': ...}，否则返回 None
        """
        data = self._load_group_data(group_id)
        for entry in data:
            if entry.get('bvid') == bvid:
                return {
                    'first_sharer': entry['first_sharer'],
                    'timestamp': entry['timestamp'],
                    'count': entry['count'],
                }
        return None

    def update_video_storage(self, group_id: str, bvid: str, sender_id: str):
        """
        新增某群组中某视频的分享记录（仅在不存在时写入）

        :param group_id: 群组 ID
        :param bvid: 视频 BV 号
        :param sender_id: 发送者 ID（作为 first_sharer）
        """
        data = self._load_group_data(group_id)

        for entry in data:
            if entry.get('bvid') == bvid:
                logger.warning(f'[DataManager] bvid={bvid} 在群 {group_id} 中已存在，跳过写入')
                return

        new_entry = {
            'bvid': bvid,
            'first_sharer': sender_id,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'count': 1,
        }
        data.append(new_entry)
        self._save_group_data(group_id, data)
        logger.info(f'[DataManager] 已记录 bvid={bvid} 由 {sender_id} 首次分享于群 {group_id}')