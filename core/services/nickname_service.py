"""
ID-昵称绑定服务 (NicknameAnchorService)

核心原则：
- 不可变锚定：当某个用户 ID 第一次被插件记录时，记录其当时的昵称为 first_nickname。
- 后续无论用户如何修改 QQ 昵称或群名片，向模型输入提示词、记录记忆、更新好感度时，
  均统一使用该固定昵称，防止模型认知漂移。
- 支持管理员手动查看、修改或重置指定用户的固定昵称。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger


class NicknameAnchorService:
    def __init__(self, data_dir: Path):
        self.file_path = Path(data_dir) / "user_identities.json"
        self._identities: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[NicknameAnchor] 加载用户身份绑定表失败: {e}")
            return {}

    def _save(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._identities, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"[NicknameAnchor] 保存用户身份绑定表失败: {e}")
            return False

    def get_anchored_nickname(self, user_id: str, raw_nickname: str = "") -> str:
        """
        获取用户的固定昵称。
        若用户首次出现，以当前 raw_nickname 登记并持久化；
        若用户已存在，更新其 last_seen_nickname（记录轨迹），但始终返回首次登记的 first_nickname。
        """
        user_id = str(user_id or "").strip()
        if not user_id:
            return raw_nickname or "对方"

        raw = str(raw_nickname or "").strip()
        now = datetime.now().isoformat()

        record = self._identities.get(user_id)
        if record is None:
            # 首次登记
            first_name = raw if raw else f"用户{user_id[-4:]}"
            self._identities[user_id] = {
                "first_nickname": first_name,
                "registered_at": now,
                "last_seen_nickname": raw or first_name,
                "last_seen_at": now,
            }
            self._save()
            logger.info(f"[NicknameAnchor] 首次登记用户 {user_id} 固定昵称: {first_name}")
            return first_name

        # 已经登记过，检查是否需要记录最新的轨迹名称
        changed = False
        if raw and record.get("last_seen_nickname") != raw:
            record["last_seen_nickname"] = raw
            changed = True
        record["last_seen_at"] = now
        if changed:
            self._save()

        return str(record.get("first_nickname") or raw or f"用户{user_id[-4:]}")

    def set_anchored_nickname(self, user_id: str, new_nickname: str) -> bool:
        """管理员手动修改/设定指定用户的固定昵称"""
        user_id = str(user_id or "").strip()
        new_name = str(new_nickname or "").strip()
        if not user_id or not new_name:
            return False

        now = datetime.now().isoformat()
        record = self._identities.get(user_id)
        if record is None:
            self._identities[user_id] = {
                "first_nickname": new_name,
                "registered_at": now,
                "last_seen_nickname": new_name,
                "last_seen_at": now,
                "manually_set": True,
            }
        else:
            record["first_nickname"] = new_name
            record["manually_set"] = True
            record["updated_at"] = now

        success = self._save()
        if success:
            logger.info(f"[NicknameAnchor] 手动设定用户 {user_id} 固定昵称为: {new_name}")
        return success

    def reset_anchored_nickname(self, user_id: str) -> bool:
        """重置指定用户的固定昵称（下次发言时将重新以当时昵称登记）"""
        user_id = str(user_id or "").strip()
        if user_id in self._identities:
            del self._identities[user_id]
            self._save()
            logger.info(f"[NicknameAnchor] 已重置用户 {user_id} 的固定昵称记录")
            return True
        return False

    def get_all_identities(self) -> Dict[str, Dict[str, Any]]:
        """获取所有身份绑定记录副本"""
        return dict(self._identities)

    def anchor_event(self, event: Any) -> str:
        """
        在事件进入流水线时，快速锚定发送者昵称，
        并尝试同步更新 event.message_obj.sender 中的相关字段。
        """
        try:
            sender_id = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
            raw_name = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
            anchored = self.get_anchored_nickname(sender_id, raw_name)

            # 尽可能同步修改 event.message_obj.sender 中的昵称
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj and getattr(msg_obj, "sender", None):
                sender = msg_obj.sender
                if hasattr(sender, "nickname"):
                    sender.nickname = anchored
                if hasattr(sender, "card"):
                    sender.card = anchored

            return anchored
        except Exception as e:
            logger.warning(f"[NicknameAnchor] 锚定事件昵称异常: {e}")
            return event.get_sender_name() if hasattr(event, "get_sender_name") else "对方"
