"""
Fiscok 插件服务层 (Service Layer)
将原本堆叠在 main.py 中的庞大业务逻辑解耦为独立服务组件。
"""
from .nickname_service import NicknameAnchorService
from .merge_service import MergeService
from .emotion_service import EmotionService
from .typing_service import TypingService
from .commitment_service import CommitmentTrackerService
from .presence_service import PresenceService
from .proactive_service import ProactiveService
from .progression_service import TopicProgressionService
from .clm_context_service import CLMContextService

__all__ = [
    "NicknameAnchorService",
    "MergeService",
    "EmotionService",
    "TypingService",
    "CommitmentTrackerService",
    "PresenceService",
    "ProactiveService",
    "TopicProgressionService",
    "CLMContextService",
]
