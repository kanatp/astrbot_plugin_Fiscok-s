# Instagram 订阅推送功能实现计划

## 概述

参照现有推特订阅推送架构，基于 `instaloader` 库实现 Instagram 帖子+快拍的订阅、缓存、定时推送功能。同时创建 `requirements.txt` 声明依赖，使 AstrBot 在插件加载时自动安装 `instaloader`。

## 现状分析

### 现有推特推送架构（参照模板）

- **数据流**: RSSHub 拉取 → 缓存到磁盘 → 定时推送到群聊
- **文件结构**:
  - `core/net/twitter_fetch.py` — 网络层：HTTP 请求 + XML 解析
  - `core/api/storage_apis.py` — 存储层：DataManager 管理缓存/订阅/推送记录
  - `main.py` — 编排层：调度器、指令组、消息构建
  - `_conf_schema.json` — 配置定义
- **订阅模型**: `twitter_subscriptions.json` 存储 `{twitter_id, alias, group_ids}`
- **推送机制**: APScheduler cron 定时触发，`get_twitter_cache()` 读取未推送内容后标记为已推送
- **消息格式**: QQ 转发消息（Nodes/Node），包含文本+图片

### AstrBot 依赖管理机制

AstrBot 通过插件根目录的 `requirements.txt` 自动管理依赖。在插件加载、安装、更新时，`PluginManager._ensure_plugin_requirements()` 会自动调用 `pip install -r requirements.txt`，并有核心依赖冲突保护机制。

### 当前插件缺失

- 无 `requirements.txt`（当前所有依赖均为 AstrBot 内置或标准库）
- 无 Instagram 相关代码

## 实现方案

### 1. 创建 `requirements.txt`

**文件**: `f:\astrbot\AstrBot\data\plugins\astrbot_plugin_Fiscok\requirements.txt`

```
instaloader>=4.10
```

AstrBot 在加载插件时会自动执行 `pip install -r requirements.txt`，无需额外配置。

### 2. 新建 Instagram 数据拉取模块

**文件**: `f:\astrbot\AstrBot\data\plugins\astrbot_plugin_Fiscok\core\net\instagram_fetch.py`

**职责**: 封装 instaloader 的调用，提供与 `twitter_fetch.py` 对等的接口。

**核心逻辑**:
- 初始化 `instaloader.Instaloader()` 实例，配置登录凭据
- `login_instagram(loader, username, password)` — 执行登录，缓存 session
- `fetch_instagram_posts(loader, username, manager)` — 拉取指定用户的最新帖子
  - 使用 `Profile.from_username(loader.context, username)` 获取 profile
  - 遍历 `profile.get_posts()`，检查是否已缓存（通过 shortcode 去重）
  - 提取文本（caption）、图片 URL 列表、发布时间
  - 调用 `manager.update_instagram_cache()` 缓存
- `fetch_instagram_stories(loader, username, manager)` — 拉取指定用户的快拍
  - 使用 `loader.get_stories(userids=[profile.userid])` 获取快拍
  - 快拍 24 小时后消失，需要特殊处理去重（基于 mediaid + 时间戳）
  - 调用 `manager.update_instagram_cache()` 缓存（复用同一缓存体系）

**注意事项**:
- instaloader 的请求需要间隔，避免触发 Instagram 限流（建议每次请求间隔 30-60 秒）
- 登录 session 可通过 instaloader 的 session 文件机制持久化，避免每次启动都重新登录
- 帖子和快拍使用统一的缓存格式，通过 `content_type` 字段区分

### 3. 扩展 DataManager（存储层）

**文件**: `f:\astrbot\AstrBot\data\plugins\astrbot_plugin_Fiscok\core\api\storage_apis.py`

在现有 `DataManager` 类中新增 Instagram 相关方法（复用与推特相同的模式）：

**新增目录结构**:
```
{plugin_data_root}/
  instagram_cache/                    # 新增
    {username}.json                   # 缓存索引
    {username}/
      {shortcode}/                    # 每个帖子/快拍一个目录
        content.txt                   # 内容 JSON（含本地图片路径）
        {shortcode}_0.jpg             # 下载的图片
        ...
  instagram_subscriptions.json        # 新增：订阅记录
```

**新增属性**:
- `instagram_cache_root = self.root / 'instagram_cache'`

**新增方法**（与推特对等）:
- `add_instagram_subscription(group_id, username, alias, umo)` — 添加订阅
- `remove_instagram_subscription(group_id, username)` — 移除订阅
- `get_instagram_subscriptions()` — 获取所有订阅的 username 列表
- `get_all_instagram_subscriptions()` — 获取完整订阅记录
- `get_group_instagram_subscriptions(group_id)` — 获取群组的订阅列表
- `update_instagram_cache(content)` — 缓存帖子/快拍内容（下载图片、写入索引）
- `get_instagram_cache(username)` — 获取未推送的缓存内容
- `instagram_cache_in_storage(username, shortcode)` — 去重检查

### 4. 添加配置项

**文件**: `f:\astrbot\AstrBot\data\plugins\astrbot_plugin_Fiscok\_conf_schema.json`

新增 `instagram_subscription_config` 配置块：

```json
{
  "instagram_subscription_config": {
    "describe": "Instagram 订阅相关配置",
    "type": "object",
    "items": {
      "instagram_subscription_available": {
        "describe": "是否启用 Instagram 订阅功能",
        "type": "bool",
        "default": false
      },
      "instagram_username": {
        "describe": "Instagram 登录用户名",
        "type": "string",
        "default": ""
      },
      "instagram_password": {
        "describe": "Instagram 登录密码",
        "type": "string",
        "default": ""
      },
      "instagram_push_time": {
        "describe": "Instagram 推送时间",
        "type": "list",
        "default": ["09:00", "15:00", "21:00"]
      },
      "instagram_push_cache_size": {
        "describe": "Instagram 推送缓存大小",
        "type": "int",
        "default": 50
      },
      "instagram_fetch_stories": {
        "describe": "是否拉取快拍（Stories）",
        "type": "bool",
        "default": true
      },
      "instagram_fetch_interval": {
        "describe": "拉取间隔（小时），防止限流",
        "type": "int",
        "default": 60
      }
    }
  }
}
```

### 5. 扩展 main.py（编排层）

**文件**: `f:\astrbot\AstrBot\data\plugins\astrbot_plugin_Fiscok\main.py`

**5a. 初始化部分（`__init__`）**:
- 导入 `instagram_fetch` 模块
- 初始化 `instaloader.Instaloader()` 实例并登录（如果配置了凭据）
- 启动 Instagram 缓存更新的后台任务 `asyncio.create_task(self.instagram_cache_update())`
- 为 Instagram 推送注册 APScheduler cron 任务

**5b. 缓存更新循环**:
```python
async def instagram_cache_update(self):
    while self.running:
        await asyncio.sleep(interval)  # 从配置读取
        if config.instagram_subscription_available:
            for username in subscriptions:
                await asyncio.sleep(fetch_interval)
                await fetch_instagram_posts(self.loader, username, self.data_manager)
                if config.instagram_fetch_stories:
                    await fetch_instagram_stories(self.loader, username, self.data_manager)
```

**5c. 推送逻辑**:
```python
async def instagram_scheduled_push(self):
    # 与 twitter_scheduled_push 完全对等的逻辑
    # 遍历订阅 → 构建 Nodes 转发消息 → 发送到各群
```

**5d. 消息构建**:
```python
def _instagram_quote_info_create(self, alias, username) -> Nodes | None:
    # 与 _quote_info_create 对等
    # 构建转发消息：标题节点 + 内容节点（文本+图片）
```

**5e. 指令组**:
```python
@filter.command_group('instagram_manager', alias={'ins管理'})
def instagram_manager(self):
    pass

# 用户指令
subscribe(username, alias)    # 订阅
unsubscribe(username)          # 取消订阅
list()                         # 订阅列表

# 管理员指令
trigger_cache_update()         # 手动缓存更新
trigger_scheduled_push()       # 手动推送
check_available()              # 检查连接/登录状态
```

### 6. 修改 `create_folder` 初始化

**文件**: `f:\astrbot\AstrBot\data\plugins\astrbot_plugin_Fiscok\core\api\storage_apis.py`

在 `DataManager.create_folder()` 和 `__init__` 中添加 `instagram_cache_root` 目录的创建。

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `requirements.txt` | 新建 | 声明 `instaloader>=4.10` 依赖 |
| `core/net/instagram_fetch.py` | 新建 | Instagram 数据拉取模块 |
| `core/api/storage_apis.py` | 修改 | 添加 Instagram 缓存/订阅管理方法 |
| `main.py` | 修改 | 添加调度、指令组、推送逻辑 |
| `_conf_schema.json` | 修改 | 添加 Instagram 配置项 |

## 关键设计决策

1. **依赖声明**: 通过 `requirements.txt` 让 AstrBot 自动安装 instaloader，无需手动干预
2. **复用模式**: 严格遵循推特推送的架构模式，保持代码一致性
3. **限流防护**: 配置化的拉取间隔，避免 Instagram 封禁账号
4. **Session 持久化**: 利用 instaloader 内置的 session 文件机制，避免每次启动都重新登录
5. **帖子+快拍统一缓存**: 使用 `content_type` 字段区分，共享同一缓存体系和推送机制

## 验证步骤

1. 确认 `requirements.txt` 创建后，重启插件时 instaloader 自动安装
2. 配置 Instagram 账号密码后，检查登录状态
3. 订阅一个公开 Instagram 账号，手动触发缓存更新，检查缓存文件
4. 手动触发推送，验证群聊收到转发消息（文本+图片）
5. 配置定时推送时间，等待自动推送验证
6. 测试快拍拉取功能（需要登录账号）
