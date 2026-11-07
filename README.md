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

开启 `suzu_bridge_config.available` 后，插件会额外启动一个独立的 aiohttp 服务（与 AstrBot 面板端口无关），供桌面端模型读取人设、记忆并把聊天交给同一个模型：

- 接口：`GET /suzu/health`、`GET /suzu/bootstrap`、`POST /suzu/chat`、`GET /suzu/memory`、`POST /suzu/memory/note`、`POST /suzu/persona/evolve`
- 鉴权：除 `/suzu/health` 外都需要 `Authorization: Bearer <token>`；未配置 token 且未配置白名单时，`/suzu` 接口只接受本机请求（WebUI 有独立的密码会话，仍可远程访问）
- 来源限制：`allowed_ips` 支持 CIDR 白名单（留空不限制）；`trusted_proxies` 决定哪些地址发来的 `X-Forwarded-For` / `X-Real-IP` 会被采信，防止客户端伪造来源绕过白名单
- 桌面端默认用 `default_user_id`（默认 `desktop_suzu`）保存个人记忆与好感度；在 WebUI 的 Live2D 标签页填「绑定 QQ 号」后，桌面端改为直接读写该 QQ 账号的个人长期记忆与好感度，和私聊完全一致（只输数字，留空即不绑定）；人设基线可在 Live2D 标签页选「跟随 AstrBot」（该 QQ 私聊实际生效的人格，含配置档案与会话级覆盖），或直接指定某个人格（如 `FisTohu`）与 AstrBot 人格设定同步；演化自我设定与情绪状态同样两边共享
- 桌面聊天会复用插件的人设叠加、情绪/好感度与个人记忆注入，并把桌面对话计入人设演化

#### LLM 对 Live2D 的控制范围

插件侧不直接编排动作，而是把回复与状态整理成客户端可直接消费的控制块。`POST /suzu/chat` 返回：

```json
{
  "reply": "好呀，今天想聊什么？",
  "emotion": "开心",
  "affinity": 62,
  "live2d": {
    "emotion": "开心",
    "affinity": 62,
    "expression": "smile",
    "motion": "",
    "segments": ["好呀，", "今天想聊什么？"]
  }
}
```

- `expression` / `motion` 由 WebUI Live2D 标签页里配置的「关键词 → 表情 / 动作」映射按当前情绪匹配得出；客户端识别不到对应表情时直接忽略即可
- `segments` 按换行切分，供桌宠逐条气泡显示或逐句 TTS
- `GET /suzu/bootstrap` 的 `live2d` 字段返回当前可用映射与支持的控制项（`expression` / `motion` / `segments`），客户端可据此校验模型能力
- 若桌面端自带更细的姿态系统，可继续用本地 Brain 兜底；桥接只提供插件侧能确定的状态

### Fiscok_webui（类 NapCat 登录）

桥接服务同时提供 `Fiscok_webui`（密码登录的插件管理面板），与 `/suzu` 共用监听端口：

- 默认监听 `127.0.0.1`，只有本机能访问；要从其他机器访问，在插件配置把 `suzu_bridge_config.host` 改为 `0.0.0.0`（面板密码仍是第一道防护），或通过反向代理转发
- 访问 `http://<插件地址>:6199/`，用随机生成的本机密码登录
- 首次启动随机生成 10 位密码，写入插件数据目录 `suzu_webui.json`，并打印在 AstrBot 本机日志（与 NapCat 在控制台打印 token 一致）
- 页面按标签页划分：概览 / Live2D / 记忆 / 表情包 / 情绪好感 / 插件配置
- Live2D 标签页集中管理桌面端相关的一切：访问令牌、来源 IP 白名单、可信反向代理、桌面聊天 Provider、默认称呼与记忆身份、情绪 → 表情 / 动作映射，并提供「重启服务」；监听地址与端口属于启动引导配置，仍在插件配置里修改
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