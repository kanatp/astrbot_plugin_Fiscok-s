# 鱼饼自用的小插件 (astrbot_plugin_Fiscok)

一个 AstrBot 自用插件集合，当前包含：B 站搬运重复统计、Twitter / Instagram 订阅推送、表情包学习、情绪与好感度系统、回复分段发送、给消息贴 QQ 表情、连续消息合并、话题记忆与群聊话题跟踪、个人长期记忆、主动发起聊天、人设演化。

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
- 每次请求都会把「当前情绪 + 对当前用户的好感度 + 关系程度」注入 system_prompt，并明确要求体现在语气、称呼、亲近程度与话题选择上；主动发言（群聊/私聊）也会带上情绪，私聊额外带上好感度；
- 好感度初始值、上下限与单次变化幅度均可配置。

### 6. 回复分段发送
将 LLM 的一次回复按换行拆分为多条消息，并按设定间隔依次发送，模拟真人分段聊天的节奏。

### 7. 群聊话题跟踪
机器人被 @/唤醒并回复后，会以这次发言开启一个话题；在话题存续期间（默认 90 秒内仍有新消息），由辅助 LLM 逐条判断群友的新发言是否与话题相关：
- 相关且值得接话 → 由模型判定是否真的接话（宁可少说、不抢话、不连续刷屏），避免同一话题里回复过于密集；
- 无关 → 不发言，从这时开始计时（默认 180 秒）；期间只要有人把话题接回来就重置计时，持续到时仍没续上才结束，避免热闹群里一段无关闲聊把话题掐掉；
- 模型认为话题已收尾 / 对方转向新话题 → 同样只进入「无关计时」，不立即退出；期间话题被重新接上则继续参与。

被 @/唤醒后的首次发言与主动发言走同一条「开启话题」链路。

另设最大续聊轮数（默认 8）与空闲超时，避免长时间占用或无限接话。判定使用关闭思考的辅助模型，判定失败时不发言。

### 8. 主动发起聊天（冷场救场）
不依赖有人 @ 机器人，当群聊安静一段时间后，由机器人自己找话题开口：
- 候选范围：只包含机器人真正发过言的群，可用白名单收窄；
- 时机：冷场超过下限（默认 30 分钟）后，在随机区间内择机，且每天有次数上限与发言冷却；
- 作息：安静时段（默认 0~7 点）不发言，提示词中会带上当前星期与时段；
- 内容：结合群聊最近记录、自己最近说过的话、人格设定生成 1~2 句口语化消息，优先顺着最近的聊天接话；
- 读空气：模型可输出 `[SKIP]` 表示此刻不该开口，插件会推迟下次尝试；
- 分段：若开启换行分段发送，会按换行拆条发送；若同时开启话题跟踪，这次发言会作为话题起点。

私聊主动（陪伴向）另有独立开关与用户白名单（QQ 号）：白名单留空表示不限制（对所有和机器人聊过天的用户生效），填写后只对这些用户生效。
- 间隔更长（默认安静 2~6 小时才可能开口，每天最多 3 次），避免打扰；
- 生成时使用陪伴向提示词：关心近况、接着上次话题、分享自己的小事，禁止查岗式追问；
- 同样支持 `[SKIP]` 读空气、安静时段与情绪/好感度注入。

### 9. 人设演化（模型自己修订人设）
在原生人设之外，插件维护一份可演化的「自我设定」（语气习惯、偏好、口头禅、相处方式），随聊天自动小幅修订：
- 机器人每回复 N 条（默认 10）后，后台用辅助模型回看最近对话，判断是否需要微调；
- 只允许调整表达习惯与偏好，不允许改动身份、世界观、底线与安全规则；
- 设定长度受限（默认 200 字），演化后持久化，并随下一轮请求注入，与原生人设叠加生效；
- 提供 `人设管理 查看 / 演化一次 / 重置` 指令，可随时查看与回滚；
- 不改动 AstrBot 数据库里的全局 persona，避免影响其他会话。

### 10. 给消息贴 QQ 表情
对别人发来的消息"随手点个表情回应"，更有活人感：每条收到的消息按概率（默认 0.08，建议 0.1 以下）触发。
- 候选表情可配置（中文名或数字 ID，逗号分隔），每次从中随机取一个；
- 默认只在**话题进行中**贴表情（`only_in_topic`）：机器人正在参与某个话题时，对群友的新消息随手回应，即使这一句没接话也有在场感；
- 跳过机器人自己的消息、空消息与斜杠指令；
- 通过协议端 `set_msg_emoji_like` 接口实现（NapCat / Lagrange 等支持）；协议端不支持时只记 debug 日志，不影响聊天；
- 不改变机器人自己的回复内容，只对原消息做表情回应。

### 11. 连续消息合并（把分几段说的话当成一段）
人在聊天时常常一句话分几条发，模型逐条回复会显得"没听懂"。开启后：
- 收到消息后先等一小会儿（默认 1.2 秒，可配置）；
- 等待期间如果又来了新消息，前一条**不单独回复**，由最后一条把几条内容合并成一段交给模型；
- 合并后模型看到的是同一个人连续说的完整意思，回复更连贯；
- 默认只对私聊生效，可关闭 `private_only` 让群聊里同一人的连续发言也合并；
- 代价是私聊回复会多等约一个 `debounce_seconds`。

### 12. 个人长期记忆（仅私聊）
参考 Mem0 / LangMem 一类方案里的「语义记忆 + 情景记忆」与原子事实（Atomic Facts）做法，为每位私聊用户维护一份小规模结构化记忆：
- **记什么**：只记稳定、长期有用的事实——档案（身份/称呼）、偏好与厌恶、重要经历、长期计划、和机器人的关系梗；一次性寒暄、临时情绪不记
- **怎么记**：每收到 N 条私聊消息（默认 6）后台调用一次辅助模型，输出 `add / update / delete` 操作；新事实与已有记忆相似度达到阈值时自动合并为更新，过时或被否认的删除
- **规模控制**：每位用户上限默认 200 条（配置项 `max_items_per_user`，只服务单人时可调到 500+），超出时淘汰重要度低、较旧的条目；单条约 20 字，模型被要求只写一个原子事实
- **怎么用**：每次私聊请求只用当前消息做向量检索，命中才注入 top 3~4 条、注入总量默认 ≤220 字，不相关就不注入，避免长期占用上下文
- **纠错**：用户指出记错或要求更正时会跳过抽取周期立即处理——能确定正确内容就更新原条目，只是要求删掉就删除；也可手动 `个人记忆 删除 <id 或关键词>`
- **指令**：`个人记忆 查看` / `个人记忆 数量` / `个人记忆 删除 <id 或关键词>` / `个人记忆 清空`（只作用于本人）

## 性能设计

情绪/好感度分析与表情包学习均在**后台异步执行**，不阻塞主对话请求流程；二者使用**独立的辅助 Provider 实例并自动关闭思考模式**（按 Anthropic / Gemini / OpenAI 兼容家族自适应），避免额外推理耗时叠加到对话延迟上。

Twitter 拉取与推送的节流间隔均可配置，且只在账号 / 群聊之间等待（首个不等待）；拉取过程复用同一个 HTTP 会话，减少连接建立开销。

### Token 节省
- **注入瘦身**：表情包占位符说明、情绪/好感度状态均压缩为单行短文本，随每轮请求重复发送的固定开销大幅下降。
- **话题注水受控**：记忆按会话隔离，仅注入相似度达标且条数受限（`retrieve_top_k`）的条目，单条按 `max_content_chars`、整批按 `max_injection_chars` 截断。
- **上下文接管**：开启 `context_optimize_config` 后，插件在 `on_llm_request` 阶段裁剪 `req.contexts`（按最近条数 / 字符上限），弥补 AstrBot 仅按"轮数"截断的不足；默认关闭，不改变原行为。
- **辅助调用节流**：表情包学习概率随库容量衰减；话题记忆抽取按 `analyze_probability` 采样；均为后台异步，不占用主对话上下文。

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
| `persona_manager show` | `人设管理 查看` | 查看当前演化出的自我设定 | 用户 |
| `persona_manager evolve` | `人设管理 演化一次` | 立即基于本会话近期对话演化一次 | 管理员 |
| `persona_manager reset` | `人设管理 重置` | 清除自我设定，回到原生人设 | 管理员 |
| `topic_memory_manager list` | `话题记忆管理 列表` | 查看当前会话的话题记忆（含 id，群聊显示归属 QQ） | 用户 |
| `topic_memory_manager delete <关键词或id>` | `话题记忆管理 删除` | 删除一条话题记忆 | 用户 |
| `topic_memory_manager count` | `话题记忆管理 数量` | 查看当前会话与合计条数 | 用户 |
| `topic_memory_manager clear` | `话题记忆管理 清空` | 清空当前会话的话题记忆 | 管理员 |
| `personal_memory_manager list` | `个人记忆 查看` | 查看自己的个人记忆（含 id） | 用户 |
| `personal_memory_manager delete <关键词或id>` | `个人记忆 删除` | 删除一条个人记忆 | 用户 |
| `personal_memory_manager count` | `个人记忆 数量` | 查看个人记忆条数 | 用户 |
| `personal_memory_manager clear` | `个人记忆 清空` | 清空自己的个人记忆 | 用户 |

> `图库管理`（`gallery_manager`）为预留指令组，暂未实现子指令。

## 配置说明

- `meme_config`：表情包功能开关、缓存大小、偷取概率上下限、注入提示概率、看图描述所用多模态 Provider（`multimodal_provider_id`，仅学习链路）、占位符标签名。
- `emotion_config`：情绪与好感度系统开关、分析所用 LLM Provider（建议使用独立轻量模型）、分析温度、初始情绪、好感度初值 / 上下限 / 单次变化上限。
- `segmented_parser_config`：换行分段解析开关与发送间隔。
- `message_merge_config`：连续消息合并开关、是否只合并私聊、等待多久确认没有后续消息（秒）、一次最多合并条数。
- `face_config`：给收到的消息贴表情回应的开关、是否只在话题进行中触发、触发概率（建议 0.1 以下）、候选表情列表（中文名或数字 ID）。
- `twitter_subscription_config`：Twitter 订阅开关、缓存大小与更新间隔、推送时间点、RSSHub 地址与端口，以及拉取账号间隔 `twitter_fetch_interval`（秒）与推送群聊间隔 `twitter_push_interval`（秒）。
- `instagram_subscription_config`：Instagram 订阅开关、推送时间点、缓存大小、是否拉取快拍、拉取间隔。
- `topic_memory_config`：话题记忆开关、Embedding Provider（手填 ID）、抽取辅助 LLM、全会话与单会话条数上限、相似度下限、检索条数、合并阈值、抽取概率、单次操作数、单条与总注入字符上限。每条记忆按发言人 QQ 号归属，昵称变化不影响身份。
- `personal_memory_config`：个人长期记忆开关（仅私聊）、抽取用辅助 LLM、Embedding Provider、抽取周期（每多少条消息）、每次最多操作数、每用户记忆上限、检索条数与相似度下限、合并阈值、单条与总注入字符上限。
- `topic_tracking_config`：群聊话题跟踪开关、续聊判定辅助 LLM、空闲超时（秒）、最大续聊轮数、无关内容持续多久后结束（秒），以及判定时携带的最近消息条数与截断长度。
- `proactive_chat_config`：主动发起聊天开关、生成辅助 LLM、冷场下限/上限（分钟）、安静时段、开口概率、每日上限、发言冷却、参考消息条数与截断、单条字符上限、群聊白名单；另有私聊主动开关、私聊用户白名单（QQ 号，留空为全部）、私聊冷场下限/上限与私聊每日上限。
- `context_optimize_config`：上下文精简开关、保留最近消息条数、上下文总字符上限（默认关闭）。
- `persona_evolution_config`：人设演化开关、演化辅助 LLM、每多少条回复尝试演化、参考对话条数与单条截断、自我设定最大字符数（默认关闭）。
- `suzu_bridge_config`：Live2D 桌面端与 WebUI 的启用开关、监听地址（默认 `127.0.0.1` 仅本机）与端口；令牌、白名单、桌面聊天 Provider、绑定 QQ、桌面身份与表情映射等其余设置全部放在 WebUI 的 Live2D 标签页。

## 桌面端桥接（Live2D 模型）

开启 `suzu_bridge_config.available` 后，插件会额外启动一个独立的 aiohttp 服务（与 AstrBot 面板端口无关），供桌面端模型读取人设、记忆并把聊天交给 AstrBot（默认走主 Agent 管线，也可切换为插件直连 Provider）：

- 接口：`GET /suzu/health`、`GET /suzu/bootstrap`、`POST /suzu/chat`、`POST /suzu/action-result`、`POST /suzu/companion/sync`、`GET /suzu/memory`、`POST /suzu/memory/note`、`POST /suzu/persona/evolve`
- 鉴权：除 `/suzu/health` 外都需要 `Authorization: Bearer <token>`；未配置 token 且未配置白名单时，`/suzu` 接口只接受本机请求（WebUI 有独立的密码会话，仍可远程访问）
- 来源限制：`allowed_ips` 支持 CIDR 白名单（留空不限制）；`trusted_proxies` 决定哪些地址发来的 `X-Forwarded-For` / `X-Real-IP` 会被采信，防止客户端伪造来源绕过白名单
- 桌面端默认用 `default_user_id`（默认 `desktop_suzu`）保存个人记忆与好感度；在 WebUI 的 Live2D 标签页填「绑定 QQ 号」后，桌面端改为直接读写该 QQ 账号的个人长期记忆与好感度，和私聊完全一致（只输数字，留空即不绑定）
- 对话链路在 Live2D 标签页选择：
  - `astrbot`（默认）：把桌面消息注入 AstrBot 事件队列，umo 对齐绑定 QQ 的私聊会话，人格、工具、技能、上下文、插件钩子与白名单全部复用 AstrBot 自己那一套；插件不再手工拼提示词，绑定 QQ 后桌面与 QQ 私聊共享同一份对话历史与演化内容
  - `direct`：插件直连 Provider，只使用本页配置的「桌面聊天 Provider」「人设基线人格」与插件侧的注入（情绪/好感度/个人记忆/演化）
- `direct` 链路的人设基线为标签页选定的 AstrBot 人格（如 `FisTohu`）；「演化自我设定」是全局共享内容，绑定 QQ 后桌面对话并入该 QQ 私聊的演化对话池
- 管线模式会继承 QQ 会话的回复装饰（`reply_prefix`、AstrBot 自带分段回复、TTS / 文转图）：若开启文转图，回包可能是图片而不是文本；若开启了 AstrBot 会话白名单（非 WebChat 平台不豁免），需保证绑定 QQ 的私聊 umo 在白名单内

#### 桌面协议 v2

桥接按 `erdi_pet/docs/astrbot-bridge.openapi.json` 的 v2 协议对接：

- `GET /suzu/health` 返回 `protocolVersion: 2`，并在 `features` 里声明 `visionInput` / `structuredActions` / `actionReceipts` / `actionCatalog`；`visionInput` 只有在确实配置了支持图片的 Provider 时才为 `true`
- `POST /suzu/chat` 接收 `requestId`、`sessionId`、`history`、`capabilities`、`environment`（含窗口信息与截图），返回 `reply`、`actions`、`emotion`、`affinity`、`visionUsed`、`warnings`
- `requestId` 为 8–80 位字母/数字/下划线/连字符，服务端五分钟内只处理一次，重复请求返回 `409 DUPLICATE_REQUEST`；同一身份 + `sessionId` 串行处理
- 请求体上限放宽到 9MiB，以容纳 base64 截图；单张图片上限 7,000,000 字符，仅接受 `image/jpeg` / `image/png`
- 图片输入默认开启，可在 WebUI 的 Live2D 标签页「允许图片输入」关闭；关闭或 Provider 不支持图片时会在 `warnings` 说明，并令 `visionUsed=false`
- `POST /suzu/action-result` 接收动作执行回执，按身份 + `sessionId` + `requestId` 校验下标与类型，五分钟内只接受一次，未知请求返回 404
- 客户端声明 `capabilities.actionPolicy`（`selection: catalog`、`emotionMapping: false`）与 `actionCatalog` 时，插件不再做任何情绪映射：动作由规划模型按目录里的 `name` 选择，目录只作为描述资料解析（条目必须属于对应白名单，意图只能引用允许的动作）
- `actions` 只会包含客户端 `capabilities` 里声明过的 `motion` / `expression`，未声明的不下发
- 客户端声明 `capabilities.features.behaviorSequence` 时，响应额外返回 `sequenceVersion: 1` 与 `sequence`（先动作、后发言，严格按步数/文字量/总时长校验）；存在 `sequence` 时它是唯一执行计划，`reply` / `actions` 仅作兼容；聊天里发言步骤固定使用主 Agent 的完整回复，规划器只决定动作与顺序
- `POST /suzu/action-result` 支持 `execution`：`steps` 必须逐一对应原始步骤下标，`spokenText` 必须是原发言的前缀；先 `accepted`、后带 `execution` 的同 `requestId` 上报不会丢失

#### 主动陪伴（主动协议 v1）

桥接按 `erdi_pet/docs/COMPANION_EVENTS.md` 实现了主动陪伴，与聊天 v2 并存：

- `GET /suzu/health` 声明 `features.proactiveEvents=true`，桌面端据此启用出站同步；主动路由真的会把截图交给支持图片的 Provider 时才声明 `features.proactiveVisionInput=true`
- `POST /suzu/companion/sync` 接收 `events` / `receipts` / `state` / `capabilities`，返回 `ackEventIds`、`ackReceiptIds` 与最多 8 条待执行 `commands`；`requestId` 原样回传
- 事件按 `id` 去重（每实例最多 512 条），只对 30 秒内的新事件决策；同类型两秒限频由桌面端负责
- 默认策略：停留/悬停优先安静看向鼠标（10 秒注意力冷却），点击、输入恢复、启动、机会事件选择看向前方 + 微笑 + 点头/轻微摆动（45 秒动作冷却）
- 决策分两次调用：第一阶段用人设 + 屏幕资料生成一句发言草稿（语气由人设决定），第二阶段用独立提示词把草稿编排成动作序列 JSON；悬停只做注视、不请求模型
- 触摸与主动发言共用两段式：触摸事件（`interaction.click` + `data.name=model.touch`）会携带部位、当前好感度（绑定 QQ 时即该 QQ 账号的好感度）、当前情绪与可用动作，先回应一句，再由第二阶段编排动作；部位名称与标签直接透传桌面端，改名或新增部位无需改插件；沿用原有冷却、发言额度、两秒静置与聊天优先，不伪造状态
- 规划侧的 Provider 仍在插件配置「主动陪伴」维护（`planner_provider_id`，留空复用桌面聊天 Provider）；插件内全部提示词统一放在 WebUI「提示词」页（`prompt_library.json`）；第二阶段超时或输出不合法时退回第一阶段草稿，不编造内容
- 同步可携带 `environment`（窗口、文字、截图）：超过 30 秒的屏幕资料直接丢弃；截图仅接受 `image/jpeg`、`capturedAt` 一分钟内、解码后 ≤5MB，通过校验才交给 Provider
- 声明 `capabilities.features.behaviorSequence` 时下发 `sequenceVersion: 1` 与 `sequence`；主动序列最多 4 个动作、400 字、总阻塞 45 秒，`can_speak=false` 时自动去掉发言步骤
- 主动话语在收到 `accepted` 回执后写回 AstrBot 同一会话历史（按 `commandId` 幂等）；序列只在执行回执的 `spokenText` 确认后保存实际说过的内容，同 ID 的后续执行回执不会被丢弃
- `kind=cancel` 可取消本实例尚未回执的指令；用户发起聊天时，该会话所有未下发的主动指令会被丢弃（聊天优先）
- 服务端预算按身份 + `sessionId` 维护：说话间隔 10 分钟、每小时最多 4 次
- `enqueue` / `cancel` / `locate` 对外开放，便于插件内其他能力主动入队；事件、指令、预算均为内存驻留，单进程有效

#### LLM 对 Live2D 的控制范围

按 `erdi_pet/docs/DIRECT_ACTION_CATALOG.md` 实现：插件不做情绪 → 表情 / 动作映射，动作完全由规划模型按桌面端上报的动作目录选择。`POST /suzu/chat` 返回：

```json
{
  "reply": "好呀，今天想聊什么？",
  "emotion": "开心",
  "affinity": 62,
  "sequenceVersion": 1,
  "sequence": [
    {"type": "action", "action": {"type": "motion", "name": "curiousLook", "mode": "interrupt"}, "waitForCompletion": true},
    {"type": "say", "text": "好呀，今天想聊什么？", "holdMs": 1600}
  ]
}
```

- 两阶段决策与主动陪伴一致：聊天回复本身就是第一阶段草稿，第二阶段由规划模型按目录决定动作与顺序；发言步骤固定使用主 Agent 的完整回复，不改写、不删减
- 规划失败、模型不服从 JSON 或动作不在目录内时只返回真实回复的 `say`，不伪造动作，也不会回退到情绪映射
- 动作目录与选择策略按 `sessionId` 保存在插件进程内，最近 32 个会话，同一会话重新上报即覆盖；WebUI 的 Live2D 标签页「已同步的动作目录」可以直接查看动作 / 表情 / 意图名称、标题、时长与循环标记
- `GET /suzu/health` 的 `features.actionCatalog=true` 表示插件支持按目录选动作
- 若桌面端自带更细的姿态系统，可继续用本地 Brain 兜底；桥接只提供插件侧能确定的状态

### Fiscok_webui（类 NapCat 登录）

桥接服务同时提供 `Fiscok_webui`（密码登录的插件管理面板），与 `/suzu` 共用监听端口：

- 默认监听 `127.0.0.1`，只有本机能访问；要从其他机器访问，在插件配置把 `suzu_bridge_config.host` 改为 `0.0.0.0`（面板密码仍是第一道防护），或通过反向代理转发
- 访问 `http://<插件地址>:6199/`，用随机生成的本机密码登录
- 首次启动随机生成 10 位密码，写入插件数据目录 `suzu_webui.json`，并打印在 AstrBot 本机日志（与 NapCat 在控制台打印 token 一致）
- 页面按标签页划分：概览 / Live2D / 提示词 / 记忆 / 表情包 / 情绪好感 / 插件配置
- Live2D 标签页集中管理桌面端相关的一切：访问令牌、来源 IP 白名单、可信反向代理、桌面聊天 Provider、默认称呼与记忆身份、是否允许图片输入，并展示桌面端最近上报的动作目录，提供「重启服务」；监听地址与端口属于启动引导配置，仍在插件配置里修改
- 提示词标签页统一维护插件内全部提示词：Live2D 发言/触摸/行为规划、人设演化与压缩、情绪与好感度、话题记忆、群聊续聊、主动聊天、个人记忆、表情包；保存到插件数据目录 `prompt_library.json`，清空文本保存即恢复内置默认，AstrBot 插件配置里不再出现这些提示词
- 概览：桥接运行状态、桌面端最近连接 / 聊天时间；支持手动「演化一次」
- 记忆：话题记忆按会话切换、私聊记忆按用户切换，均可修改内容与重要度、单条删除与清空
- 表情包库：缩略图预览、关键字搜索、按标签 / 情绪筛选，可编辑描述、标签与情绪，支持单条删除与清空
- 情绪与好感度：直接修改全局情绪文字（可选同时写为默认情绪），查看并调整每个用户的好感度、恢复默认值
- 插件配置：在线修改表情包、话题记忆、个人记忆、情绪与好感度、人设演化的开关和阈值，保存后写入 AstrBot 插件配置
- 会话使用 HttpOnly + SameSite=Strict 的签名 Cookie，12 小时过期；登录失败 5 次后封禁 10 分钟
- 桌面端 Kira Live 控制台的「AstrBot → 插件 WebUI」按钮会直接打开这个页面；桥接地址与令牌也可在页面里复制

### 反向代理 + IP 白名单

云端部署不要把 6199 直接暴露到公网。推荐让插件监听 `127.0.0.1`，由同机 nginx 负责 TLS、白名单与限流：

```nginx
# /etc/nginx/conf.d/suzu.conf
limit_req_zone $binary_remote_addr zone=suzu:10m rate=6r/m;

server {
    listen 443 ssl http2;
    server_name suzu.example.com;

    ssl_certificate     /etc/letsencrypt/live/suzu.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/suzu.example.com/privkey.pem;

    # 只允许家里/公司等固定出口访问；动态家宽请配合 DDNS 或收紧到运营商网段
    allow 203.0.113.7;
    allow 2001:db8::/32;
    deny all;

    # `/` 是插件 WebUI，`/suzu/` 是桌面桥接，同一端口一起转发
    location / {
        limit_req zone=suzu burst=6 nodelay;

        proxy_pass http://127.0.0.1:6199;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 令牌由桌面端自带，这里只透传，不要写进配置
        proxy_set_header Authorization $http_authorization;
        proxy_read_timeout 90s;
    }
}
```

配套的插件配置（只有启动引导项）：

```json
"suzu_bridge_config": {
  "available": true,
  "host": "127.0.0.1",
  "port": 6199
}
```

`allowed_ips`、`trusted_proxies` 与访问令牌改到 WebUI 的 Live2D 标签页里配置。`allowed_ips` 在反代模式下只需填代理自身的地址（同机即 `127.0.0.1`），真实客户端地址由 nginx 的 `X-Forwarded-For` 传入；nginx 的 `allow/deny` 才是面向公网的客户 IP 白名单。桌面端 Console 的地址填 `https://suzu.example.com/suzu`，令牌与 WebUI 里保存的一致。

#### 公网访问排查（502 / 连接失败）

`502 Bad Gateway` 不是本插件返回的，它来自前面的 nginx、宝塔、frp 或其他反向代理，表示代理连不上插件。按顺序排查：

1. **确认插件真的在监听**：在服务器本机执行 `curl -i http://127.0.0.1:721/`（换成实际端口），WebUI 首页应返回 HTML；`netstat -ano | findstr :721`（Windows）或 `ss -lntp | grep 721`（Linux）应看到监听进程。
2. **看 AstrBot 日志里的 `[Fiscok's][suzu]`**：只有「正在监听 … / 桌面桥接已启动」才说明端口成功绑定；出现「端口启动失败」时按提示处理：
   - Linux/Unix 下低于 1024 的端口需要 root 权限，建议改用 1024 以上的端口；
   - 端口被其他程序占用（包括面板/nginx 已经占用了该端口）；
   - Docker 部署必须在 `docker run -p 宿主机端口:容器端口` 或 compose 的 `ports` 里映射，容器内监听不等于宿主机可达。
3. **使用 frp / 内网穿透时**：frpc 的 `local_ip` 填 `127.0.0.1`、`local_port` 与插件端口一致；frpc 没连上或本地端口没开时，frps 就会返回 502。
4. **确认改端口后已生效**：修改 `suzu_bridge_config` 保存后 AstrBot 会热重载插件；若日志仍显示旧端口，重载一次插件或重启 AstrBot。
5. 防火墙为黑名单/全放行时不需要额外开端口；若开了系统防火墙或云厂商安全组，仍需放行该端口。

## 版本说明

### v2.9
- 新增模型触摸反馈：识别 `interaction.click` + `data.name=model.touch`，第一阶段提示词直接携带触摸部位、绑定 QQ 对应好感度、当前情绪与可用动作，第二阶段仍按动作目录编排序列；部位名称/标签直接透传桌面端，改名或新增部位无需改插件；沿用既有冷却、发言额度、两秒静置与聊天优先。
- 新增提示词库（WebUI「提示词」页）：插件内全部提示词（Live2D、人设、情绪、话题记忆、群聊续聊、主动聊天、个人记忆、表情包）统一维护，落盘 `prompt_library.json`，不进插件配置。
- 人设演化超出字数上限时改为调用 LLM 压缩，模型不可用或结果为空才回退截断。

### v2.7
- 对接 OpenAPI 2.2 与 BEHAVIOR_SEQUENCE.md：聊天响应新增 `sequenceVersion` / `sequence`（按动作+发言派生），`/suzu/action-result` 支持 `execution` 且允许同 ID 二次上报。
- 主动同步支持 `environment` 屏幕资料与 `proactiveVisionInput`；主动命令新增 `sequence`，序列预算 4 动作/400 字/45 秒，`can_speak=false` 自动去掉发言；执行回执按 `spokenText` 才保存话语。

### v2.6
- 按 COMPANION_EVENTS.md 实现主动陪伴协议 v1：新增 `POST /suzu/companion/sync`，`/suzu/health` 声明 `proactiveEvents`。
- 支持事件去重、按实例排队、默认轻动作（注视/微笑/点头）、Provider 主动话语（6 秒超时保持安静）、执行回执与 `cancel`。
- 主动话语按 `commandId` 幂等写回 AstrBot 会话历史；用户聊天优先，会丢弃该会话未下发的主动指令。
- `actions` 与回执新增 `attention` 类型。

### v2.5
- 桌面桥接升级到 v2 协议：`/suzu/health` 声明协议版本与能力；`/suzu/chat` 支持 `requestId` 去重（409 `DUPLICATE_REQUEST`）、同会话串行、`capabilities` 白名单动作、`visionUsed` 与 `warnings`；新增 `POST /suzu/action-result` 动作回执（五分钟有效、只接受一次）。
- 新增桌面图片输入：截图随消息进入 Provider（管线模式注入 `Image` 组件、直连模式走 `image_urls`）；默认开启，可在 WebUI Live2D 标签页关闭；请求体上限放宽到 9MiB。
- 注入事件不再参与表情包偷取，避免桌面截图被当作表情包保存。

### v2.4
- Live2D 桌面端新增「对话链路」，默认改为 `astrbot`：把桌面消息注入 AstrBot 事件队列，由 AstrBot 主 Agent 管线处理（人格、工具、技能、上下文、插件钩子、安全策略全部复用），插件只负责收发内容；绑定 QQ 时 umo 与 QQ 私聊完全一致，共享同一份对话历史与配置。
- 管线链路下不再重复记录桌面对话/情绪（避免与插件钩子双写）；插件侧的表情包单独发送与回复分段钩子会跳过注入事件，避免把桌面回复发到真实 QQ；连续消息合并同样跳过。
- 新增桌面环境摘要注入钩子；「桌面聊天 Provider」「人设基线人格」标注为仅 `direct` 链路生效。
- 修复：WebUI 保存 Live2D 设置时遗漏 `persona_id`（选了人格实际没有落盘）。

### v2.3
- Live2D 桌面端与 WebUI 服务默认只监听 `127.0.0.1`，不再默认对外开放；监听地址与端口作为引导配置保留在插件配置 `suzu_bridge_config`，其余 Live2D 设置全部迁移到 WebUI。
- WebUI 改为标签页布局：概览 / Live2D / 记忆 / 表情包 / 情绪好感 / 插件配置；新增的 Live2D 标签页集中管理访问令牌、IP 白名单、可信反向代理、桌面聊天 Provider、默认称呼与记忆身份、情绪 → 表情 / 动作映射，并可「重启服务」。
- 调整安全策略：WebUI 有自己的密码会话，可远程访问；未配置令牌与白名单时，桌面桥接 `/suzu` 接口仅接受本机请求，不再整站强制回退到本机监听。
- `POST /suzu/chat` 新增 `live2d` 控制块（`emotion` / `affinity` / `expression` / `motion` / `segments`），`GET /suzu/bootstrap` 同步返回可用映射，桌面端可直接消费；`suzu_bridge_config` 清理为 `available` / `host` / `port` 三项。

### v2.2
- 新增**主动发起聊天（群聊冷场救场）**：随机冷场区间、开口概率、`[SKIP]` 读空气、作息时段、每日上限与发言冷却、群聊白名单；参考群聊最近记录与自己最近发言，避免复读。
- 新增**私聊主动（陪伴向）**：独立开关 + QQ 号白名单（留空为全部私聊用户），默认安静 2~6 小时才可能开口、每日最多 3 次；使用陪伴向提示词，禁止查岗式追问。
- 强化**情绪/好感度对输出的影响**：注入「情绪 + 好感度 + 关系程度」，并明确要求在语气、称呼、亲近程度与话题选择上体现；主动发言同样带情绪，私聊额外带好感度。
- 新增**人设演化**：插件侧维护一份可演化的「自我设定」，与原生人设叠加注入，模型可按周期小幅修订；只允许调整语气/偏好/口头禅等，不允许改动身份与安全规则；提供 `人设管理 查看 / 演化一次 / 重置`。
- 新增配置组 `proactive_chat_config`（含私聊相关项）与 `persona_evolution_config`。
- 修复：模型若把 AstrBot 注入的 `<system_reminder>`（用户 ID、昵称、时间等内部提示）原样复述，插件会在发送前自动清理，不再发给用户。
- 调整：话题跟踪的结束判定由「连续无关条数」改为「无关持续时间」——判定为无关/该收尾后开始计时（默认 180 秒，可配置），期间话题被续上即重置；空闲超时改为从最后一条群消息起算。
- 调整：话题跟踪是否接话不再用概率控制，完全交给判定模型决定（提示词要求"宁可少说、不抢话、不连续刷屏"）；被 @ 的首次发言与主动发言共用同一条开启话题链路。
- 新增：给别人的消息贴 QQ 表情回应（`face_config`），每条收到的消息按概率（默认 0.08）随机贴一个表情；默认只在话题进行中触发，机器人不接话时也有在场感。
- 修复：主动聊天的等待时间改为每次检查按当前配置实时计算，调短/调长冷场配置后立即生效，不再沿用启动时算好的旧等待时间。
- 新增：连续消息合并（`message_merge_config`）——对方连发几条消息时先等一小会儿，合并成一段再交给模型，避免逐条回复导致"听不懂"；默认只对私聊生效。
- 新增：个人长期记忆（`personal_memory_config`，仅私聊）——按用户维护原子事实（档案/偏好/经历/计划/关系），自动去重合并、超限淘汰，请求时只按相关度注入少量条目以控制上下文开销；提供 `个人记忆 查看/数量/删除/清空` 指令，用户指出记错时会立即重抽并纠正条目。
- 重构：话题记忆改为与个人记忆同方案——按会话隔离的原子事实、`add/update/delete` 增量操作、相似度去重合并、单会话与总量双重上限淘汰、检索时单条与整批字数上限；`话题记忆管理` 支持 `列表/删除/数量/清空`（按当前会话）。旧格式数据不再兼容（升级后自动忽略）。
- 调整：话题记忆按发言人 QQ 号归属（昵称仅作展示），群聊注入与列表会显示 `[QQ号]` 前缀；昵称变化后仍能识别为同一人，避免记忆错位。

## 依赖

- 插件依赖：见 `requirements.txt`（`instaloader`）。
- 外部服务：推特功能需部署 RSSHub；Instagram 功能需有效 cookies。

## 仓库

<https://github.com/kanatp/astrbot_plugin_Fiscok-s>