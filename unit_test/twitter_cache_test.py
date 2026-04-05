from ..core.net.twitter_fetch import fetch_twitter_data
from ..core.api.storage_apis import DataManager
from astrbot.api import AstrBotConfig

DataManager = DataManager("test_data_path", AstrBotConfig())
fetch_twitter_data("aimi_sound", DataManager)