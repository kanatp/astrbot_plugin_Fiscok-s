# 鱼饼自用的小插件 (astrbot_plugin_Fiscok)

一个 AstrBot 自用插件集合，当前包含：B 站搬运重复统计、Twitter / Instagram 订阅推送、表情包学习、情绪与好感度系统、回复分段发送。

> 本插件为个人自用，功能默认全部关闭，请在 AstrBot 插件配置中按需开启。

## 功能

### 1. B 站搬运重复统计
群内有人发送 B 站视频链接时，机器人会判断该视频是否已在本群出现过：
- 首次出现：提示"还是第一次在这里看到这个视频呢"；
- 重复出现：提示最早由谁、何时发送过，以及至今已被发送的次数。

仅处理主动发送的消息，忽略引用与转发消息。

### 2. Twitter 订阅推送
通过 [RSSHub](https://docs.rsshub.app/) 拉取订阅账号的最新动态，缓存后按设定时间批量推送到群聊（以合并转发消息展示）。
- 需自行部署 RSSHub 并配置 `rssHub_url` / `rssHub_port`；
- 支持通过指令更新 Twitter Cookie 并重启 RSSHub 容器。

### 3. Instagram 订阅推送
基于 `instaloader` + `cookies.json` 拉取订阅账号的帖子与快拍（Stories），定时推送到群聊。
- 需在 `cookies.json` 中配置 Instagram cookies（`sessionid` / `ds_user_id` / `csrftoken`），或通过指令更新。

### 4. 表情包学习
- 以一定概率"偷取"群友发送的表情包，概率随已缓存数量增多而衰减；
- 使用多模态 LLM 生成描述、标签与情绪分类后入库；
- 机器人发言时按概率携带表情包，通过在回复中输出 `meme` 占位符触发。

### 5. 情绪与好感度系统
- 维护一个**全局情绪状态**（文字描述）与每位用户的**好感度**（数值，持久化）；
- 将该状态注入 LLM 上下文，影响机器人回复的口吻与态度；
- 好感度初始值、上下限与单次变化幅度均可配置。

### 6. 回复分段发送
将 LLM 的一次回复按换行拆分为多条消息，并按设定间隔依次发送，模拟真人分段聊天的节奏。

## 性能设计

情绪/好感度分析与表情包学习均在**后台异步执行**，不阻塞主对话请求流程；二者使用**独立的辅助 Provider 实例并自动关闭思考模式**（按 Anthropic / Gemini / OpenAI 兼容家族自适应），避免额外推理耗时叠加到对话延迟上。

## 指令

| 指令 | 别名 | 说明 | 权限 |
| --- | --- | --- | --- |
| `pull_cache_test` | `拉取测试` | 测试缓存拉取 | 用户 |
| `push_cache_test` | `推送测试` | 测试缓存推送 | 用户 |
| `twitter_manager subscribe <id> [alias]` | `推特管理 订阅` | 订阅推特账号 | 用户 |
| `twitter_manager unsubscribe <id>` | `推特管理 取消订阅` | 取消推特订阅 | 用户 |
| `twitter_manager list` | `推特管理 订阅列表` | 查看本群推特订阅 | 用户 |
| `twitter_manager update_cookie <auth_token> <ct0>` | `推特管理 更新Cookie` | 更新 Twitter Cookie 并重启 RSSHub | 管理员 |
| `twitter_manager check_available` | `推特管理 检查连接状态` | 检查 RSSHub 连通性 | 管理员 |
| `twitter_manager trigger_cache_update` | `推特管理 手动缓存更新` | 手动拉取推特缓存 | 管理员 |
| `twitter_manager trigger_scheduled_push` | `推特管理 手动推送` | 手动触发推特推送 | 管理员 |
| `instagram_manager subscribe <username> [alias]` | `ins管理 订阅` | 订阅 Instagram 账号 | 用户 |
| `instagram_manager unsubscribe <username>` | `ins管理 取消订阅` | 取消 Instagram 订阅 | 用户 |
| `instagram_manager list` | `ins管理 订阅列表` | 查看本群 Instagram 订阅 | 用户 |
| `instagram_manager check_cookies` | `ins管理 检查cookies` | 检查 cookies 是否有效 | 管理员 |
| `instagram_manager reload_cookies` | `ins管理 重载cookies` | 重新加载 cookies | 管理员 |
| `instagram_manager update_cookies <sessionid> <ds_user_id> <csrftoken>` | `ins管理 更新cookies` | 更新 Instagram cookies | 管理员 |
| `instagram_manager trigger_cache_update` | `ins管理 手动缓存更新` | 手动拉取 Instagram 缓存 | 管理员 |
| `instagram_manager trigger_push` | `ins管理 手动推送` | 手动触发 Instagram 推送 | 管理员 |

> `图库管理`（`gallery_manager`）为预留指令组，暂未实现子指令。

## 配置说明

- `meme_config`：表情包功能开关、缓存大小、偷取概率上下限、发言携带概率、描述生成所用 LLM Provider、占位符标签名。
- `emotion_config`：情绪与好感度系统开关、分析所用 LLM Provider（建议使用独立轻量模型）、分析温度、初始情绪、好感度初值 / 上下限 / 单次变化上限。
- `segmented_parser_config`：换行分段解析开关与发送间隔。
- `twitter_subscription_config`：Twitter 订阅开关、缓存大小与更新间隔、推送时间点、RSSHub 地址与端口。
- `instagram_subscription_config`：Instagram 订阅开关、推送时间点、缓存大小、是否拉取快拍、拉取间隔。

## 依赖

- 插件依赖：见 `requirements.txt`（`instaloader`）。
- 外部服务：推特功能需部署 RSSHub；Instagram 功能需有效 cookies。

## 仓库

<https://github.com/kanatp/astrbot_plugin_Fiscok-s>