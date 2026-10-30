# 鱼饼自用的小插件 (astrbot_plugin_Fiscok)

一个 AstrBot 自用插件集合，当前包含：B 站搬运重复统计、Twitter / Instagram 订阅推送、表情包学习、情绪与好感度系统、回复分段发送、话题记忆与群聊话题跟踪。

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
- 机器人发言时，由**主对话模型**在回复中输出 `meme` 占位符触发；插件按情绪选图后单独发送，图片不进入对话上下文（省 token）。

### 5. 情绪与好感度系统
- 维护一个**全局情绪状态**（文字描述）与每位用户的**好感度**（数值，持久化）；
- 将该状态注入 LLM 上下文，影响机器人回复的口吻与态度；
- 好感度初始值、上下限与单次变化幅度均可配置。

### 6. 回复分段发送
将 LLM 的一次回复按换行拆分为多条消息，并按设定间隔依次发送，模拟真人分段聊天的节奏。

### 7. 群聊话题跟踪
机器人被 @/唤醒并回复后，会以这次发言开启一个话题；在话题存续期间（默认 90 秒内仍有新消息），由辅助 LLM 逐条判断群友的新发言是否与话题相关：
- 相关且值得接话 → 插件主动触发一轮回复，自然接住下一句；
- 无关 → 不发言，连续多条无关（默认 3 条）后自动结束；
- 模型判定话题已收尾 / 对方转向新话题 → 立即结束参与。

另设最大续聊轮数（默认 8）与空闲超时，避免长时间占用或无限接话。判定使用关闭思考的辅助模型，判定失败时不发言。

## 性能设计

情绪/好感度分析与表情包学习均在**后台异步执行**，不阻塞主对话请求流程；二者使用**独立的辅助 Provider 实例并自动关闭思考模式**（按 Anthropic / Gemini / OpenAI 兼容家族自适应），避免额外推理耗时叠加到对话延迟上。

Twitter 拉取与推送的节流间隔均可配置，且只在账号 / 群聊之间等待（首个不等待）；拉取过程复用同一个 HTTP 会话，减少连接建立开销。

### Token 节省
- **注入瘦身**：表情包占位符说明、情绪/好感度状态均压缩为单行短文本，随每轮请求重复发送的固定开销大幅下降。
- **话题注水受控**：仅注入相似度达标且条数受限（`retrieve_top_k`）的话题，单条内容按 `max_content_chars` 截断。
- **上下文接管**：开启 `context_optimize_config` 后，插件在 `on_llm_request` 阶段裁剪 `req.contexts`（按最近条数 / 字符上限），弥补 AstrBot 仅按"轮数"截断的不足；默认关闭，不改变原行为。
- **辅助调用节流**：表情包学习概率随库容量衰减；话题判定按 `analyze_probability` 采样；均为后台异步，不占用主对话上下文。

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

- `meme_config`：表情包功能开关、缓存大小、偷取概率上下限、注入提示概率、看图描述所用多模态 Provider（`multimodal_provider_id`，仅学习链路）、占位符标签名。
- `emotion_config`：情绪与好感度系统开关、分析所用 LLM Provider（建议使用独立轻量模型）、分析温度、初始情绪、好感度初值 / 上下限 / 单次变化上限。
- `segmented_parser_config`：换行分段解析开关与发送间隔。
- `twitter_subscription_config`：Twitter 订阅开关、缓存大小与更新间隔、推送时间点、RSSHub 地址与端口，以及拉取账号间隔 `twitter_fetch_interval`（秒）与推送群聊间隔 `twitter_push_interval`（秒）。
- `instagram_subscription_config`：Instagram 订阅开关、推送时间点、缓存大小、是否拉取快拍、拉取间隔。
- `topic_memory_config`：话题记忆开关、Embedding Provider（手填 ID）、话题判定辅助 LLM、记忆上限 / 相似度下限 / 检索条数 / 判定采样概率 / 单条注入字符上限。
- `topic_tracking_config`：群聊话题跟踪开关、续聊判定辅助 LLM、空闲超时（秒）、最大续聊轮数、连续无关消息上限，以及判定时携带的最近消息条数与截断长度。
- `context_optimize_config`：上下文精简开关、保留最近消息条数、上下文总字符上限（默认关闭）。

## 依赖

- 插件依赖：见 `requirements.txt`（`instaloader`）。
- 外部服务：推特功能需部署 RSSHub；Instagram 功能需有效 cookies。

## 仓库

<https://github.com/kanatp/astrbot_plugin_Fiscok-s>