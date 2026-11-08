# Live2D 桌面端改为走 AstrBot 事件管线

## Context

现在 `core/suzu_bridge.py` 的 `/suzu/chat` 自己拼 system prompt（人格基线 + 情绪 + 演化 + 记忆 + 桌面环境）、自己组装 history、再直接调用 `provider.text_chat`。这导致：

- 与 QQ 私聊链路不一致（工具、技能、AstrBot 人格设置、插件 `on_llm_request` / `on_llm_response` 钩子都要在插件里手工复刻）；
- 插件里已有的人格/情绪/记忆注入钩子在桌面端不生效，桌面端只能看到手工拼的那一份。

目标：像 NapCat 一样，Live2D 只负责“发消息 + 收内容”，把消息注入 AstrBot 事件队列，让 AstrBot 主 Agent 管线完整处理（人格、工具、技能、插件钩子、安全策略、上下文管理全部复用）。插件直连 Provider 保留为可选链路。

已核实的关键机制（AstrBot 本体）：

- `Context.get_event_queue()` 返回事件队列（`astrbot/core/star/context.py` L764），`EventBus.dispatch` 从队列取事件并按 `event.unified_msg_origin` 选配置/管线（`event_bus.py` L39-54）。
- `AstrMessageEvent.session` 由 `PlatformMetadata.id` 构造（`astr_message_event.py` L70-74），umo 固定为 `平台实例ID:消息类型:会话ID`。因此只要 `platform_meta.id` 取真实 QQ 平台实例 id、`session_id` 取绑定 QQ，umo 就与 QQ 私聊**完全一致**（配置、人格、上下文、白名单、钩子全部对齐）。
- `WebChatMessageEvent.send` / `send_streaming` 是把回复写进 `webchat_queue_mgr` 的请求级回包队列（`webchat_event.py` L28-263），不经过任何真实平台。
- `PipelineScheduler.execute` 在管线结束后对 `WebChatMessageEvent` 额外执行 `await event.send(None)`（`scheduler.py` L93-95），而 `_send(None)` 会写入 `{"type": "end"}`（`webchat_event.py` L37-47）——这就是可靠的**结束信号**，不需要猜超时。
- 非流式时 `send()` 只写 `plain`（`streaming=False`）；流式时写多个 `plain`（增量）+ 最后 `complete`（`data` 为完整文本）；`tool_call` 类消息带 `chain_type`，应忽略。

## 实现步骤

### 1. 新建 `core/api/pipeline_chat_apis.py`

对外只暴露一个协程：

```python
async def chat_via_astrbot(
    context, *, text, user_id, nickname,
    platform_id, platform_name, self_id="",
    environment="", first_timeout=90.0, idle_timeout=10.0,
) -> str
```

流程：

1. 构造 `AstrBotMessage`：`type=MessageType.FRIEND_MESSAGE`、`self_id`、`session_id=str(user_id)`、`message_id=request_id（uuid）`、`sender=MessageMember(str(user_id), nickname)`、`message_str=text`、`message=[Plain(text)]`、`raw_message={"post_type":"message","message_type":"private","user_id":user_id,"raw_message":text}`（必须先填齐再构造事件，`AstrMessageEvent.__init__` 会读 sender/outline）。
2. `PlatformMetadata(name=platform_name, description="Live2D 桌面端", id=platform_id)`。
3. `event = WebChatMessageEvent(text, message_obj, meta, str(user_id))`，随后：
   - `event.set_extra("_suzu_pipeline", True)`（供插件钩子识别）；
   - `event.set_extra("_suzu_environment", environment)`（若提供）。
4. `webchat_queue_mgr.get_or_create_back_queue(request_id, f"suzu:{user_id}")` → `await context.get_event_queue().put(event)`。
5. 消费回包：
   - `plain`：`streaming=True` 累加增量；`streaming=False` 作为一段完整文本收集（多段用换行连接）；
   - `complete`：`streaming=True` 时用 `data` 覆盖为最终文本；
   - `end`：结束；
   - 忽略 `run_started` / `agent_stats` / `follow_up_captured` / `chain_type` 为 `tool_call`、`tool_call_result` 的段；`image/record/file` 忽略。
6. 超时：首包 `first_timeout`（90s），有内容后 `idle_timeout`（10s）兜底；都没有则抛 `RuntimeError`。
7. `finally: webchat_queue_mgr.remove_back_queue(request_id)`。

### 2. `core/suzu_bridge.py`

- `apply_settings` 增加 `self.chat_mode = "direct" if data.get("chat_mode") == "direct" else "astrbot"`。
- 新增 `_pipeline_target()`：遍历 `context.platform_manager.get_insts()`，优先取 `inst.meta().name == "aiocqhttp"`（退化为 id 含 `qq`/`napcat`）的平台实例：
  - 返回 `(platform_id, "suzu_desktop", self_id)`；
  - `platform_id` = 该实例 `meta().id`（umo 的第一段，必须与 QQ 私聊一致）；
  - `platform_name` 固定为 `suzu_desktop`：避免把回复误判成 QQ 消息而触发 `forward_threshold`（默认 1500 字）转“合并转发”（WebChat 会丢弃 `Node`），也避免 `webchat` 专属人格分支；
  - `self_id` 取 `inst.self_id / inst.bot.self_id` 等属性，取不到用 `"0"`；
  - 未绑定 QQ 时回退 `("suzu_desktop", "suzu_desktop", "0")`（落到默认配置，管线仍可完整工作）。
- `_chat`：`chat_mode == "astrbot"` 时
  ```python
  platform_id, platform_name, self_id = self._pipeline_target()
  reply = await chat_via_astrbot(context, text=text, user_id=user_id, nickname=nickname,
                                 platform_id=platform_id, platform_name=platform_name,
                                 self_id=self_id, environment=summary)
  ```
  返回后**不再调用 `_record_dialogue`**（管线里的 `on_llm_request_emotion_hook` / `on_llm_request_persona_hook` / `on_llm_response_persona_hook` 已经记录对话、调度情绪、累计演化计数，重复调用会双写）；`affinity` 在回复后重新读取再返回。
- 直连分支（`direct`）逻辑保持原样，仅在分支条件内保留原有 prompt 拼接。
- 管线模式的异常：`RuntimeError`/超时映射为 504/502，日志统一打 `[Fiscok's][suzu]`。

### 3. `core/suzu_settings.py`

`DEFAULTS` 增加 `"chat_mode": "astrbot"`；`normalize()` 增加同样字段（只允许 `astrbot` / `direct`，其它值归一到 `astrbot`）。

### 4. `core/suzu_webui.py`

`_live2d_set` 的字段白名单循环里补 `chat_mode`，并顺带补上此前漏掉的 `persona_id`（现在 WebUI 选了人格实际没有保存）。

### 5. `core/suzu_webui.html`

- 在 `l2d-provider` 之后新增字段 `l2d-chat-mode`（select）：
  - `astrbot`：**AstrBot 管线（推荐）**——复用 AstrBot 人格、工具、技能、插件钩子与 QQ 私聊上下文；
  - `direct`：**插件直连 Provider**——只使用本页配置的人格/Provider 与插件注入。
- 给 `l2d-provider`、`l2d-persona` 的 hint 补一句“仅「插件直连」链路生效”。
- `loadLive2D()` 读 `settings.chat_mode`；`saveLive2D()` 写 `chat_mode`。
- 保持不变：保存后 `refresh()` + `loadLive2D()`。

### 6. `main.py`

- `on_decorating_result_hook`、`on_decorating_result_split_hook` 开头：`if event.get_extra('_suzu_pipeline'): return`（这两个钩子会 `context.send_message` 真发到 QQ，必须跳过）。
- `merge_fragments_on_message` 开头同样跳过：注入事件与 QQ 私聊共用 umo，不跳过会让桌面消息和 QQ 私聊片段互相合并、并引入 1.2s 延迟。
- 新增 `@filter.on_llm_request()` 的 `on_llm_request_suzu_environment_hook`：仅当 `event.get_extra('_suzu_environment')` 有值时，把 `<桌面环境>…</桌面环境>` 追加进 `req.system_prompt`（保持原桌面环境提示能力）。
- 版本号 `2.3 → 2.4`；`metadata.yaml` 同步 `v2.4`。

其余钩子保持生效（这正是要的全局一致性）：表情包不受影响（无图片）、好感度初始化、话题记忆学习、个人记忆按条数抽取、`on_llm_request` 的情绪/人格/记忆/话题注入、`on_llm_response` 演化与表情包占位符处理；`face_react_on_message` 因 `event.bot` 为 None 天然跳过。

### 7. `README.md`

- 桌面端桥接章节补充「对话链路」：默认 `astrbot`（注入事件走 AstrBot 管线，umo 对齐绑定 QQ 的私聊会话，共享人格/工具/技能/历史/白名单/插件钩子；插件不再手工拼 prompt）；`direct` 为原直连链路。
- 说明 `llm_provider_id`、`persona_id` 在 `astrbot` 链路下不参与（人格由 AstrBot 按会话解析）；未绑定 QQ 时落到默认配置。
- 版本说明新增 v2.4 条目。

## 风险与取舍

- 管线模式会继承 QQ 会话的装饰：`reply_prefix`、AstrBot 自带分段回复、TTS / 文转图。若开启文转图且回复较长，回包可能是图片而不是文本，桥接会返回“模型没有返回内容”；需要在 README 提示。
- 会话历史与 QQ 私聊共用（这是“全局一致性”的应有之义）；若开启 AstrBot 会话白名单（非 webchat 不豁免），需保证绑定 QQ 的私聊 umo 在白名单内。
- 平台上可能出现其他插件用 `context.send_message(umo, …)` 主动推送；本插件已全部加跳过标记，第三方插件无法完全隔离（与 NapCat 复用时同样的取舍）。

## 验证

1. `python -m py_compile core/api/pipeline_chat_apis.py core/suzu_bridge.py core/suzu_settings.py main.py`。
2. 检查 `suzu_webui.html` 里新字段 id、`loadLive2D` / `saveLive2D` 三处一致；JSON 结构无语法错误。
3. 启动 AstrBot（或热重载插件），日志出现 `[Fiscok's][suzu] 桌面桥接已启动`。
4. 调 `POST /suzu/chat`（带 Token）：应正常返回 `reply` 与 `live2d`；AstrBot 日志出现该 umo 的 `Prepare to send`，且**真实 QQ 不会收到桌面消息**。
5. 在 QQ 私聊里继续对话，确认上下文连续（桌面与 QQ 共享同一会话），人格仍为 FisTohu。
6. WebUI Live2D 页切换链路为 `direct` 保存后，桌面聊天回到直连 Provider 行为。
