# 换行分段解析器 + 情绪与好感度系统 实现计划

## 一、功能概述

本计划包含两个相互独立的小任务：

1. **换行分段解析器**：当 LLM 回复包含换行符（如两段话）时，在"发送前"（`on_decorating_result` 阶段）把回复内容按**每个换行符**拆分成多条消息分别发送，并跳过空行/纯空白行。
2. **情绪与好感度系统**：新增一个由**高温度模型**处理的情绪分析模块（模型获取方式与表情包系统一致），依照用户先前对LLM的回复/发起会话的内容修改LLM对于用户的好感度（不是分析用户对于模型，而是模型对用户，此处是一个拟人化处理）；好感度按用户持久化到 JSON，新用户初始化为 50；分析结果注入主 LLM 的 system_prompt，使回复语气随好感度变化。

---

## 二、现状分析

### AstrBot 框架关键流程
```
OnLLMRequestEvent(插件可改 req.system_prompt)
  → 主 Agent 调用 LLM
OnLLMResponseEvent(插件可改 resp.completion_text / result_chain)
  → 结果进入 event
OnDecoratingResultEvent(发送前，插件可改 result.chain 或清空 result)
  → ResultDecorateStage(回复前缀/分段回复/TTS/at/引用等)
  → RespondStage 发送消息链
```

- 插件已有表情包链路（[main.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/main.py)）：`on_llm_request_hook`(108 行) → `on_llm_response_hook`(252 行) → `on_decorating_result_hook`(315 行)。
- 表情包系统的 LLM 获取方式（[meme_apis.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/core/api/meme_apis.py)）：`context.get_provider_by_id(provider_id)`，为空则退回 `context.get_all_providers()[0]`，再 `provider.text_chat(prompt=..., system_prompt=..., temperature=...)`（`text_chat` 支持 `**kwargs` 透传 `temperature`）。
- 数据持久化模式（[storage_apis.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/core/api/storage_apis.py)）：`DataManager` 持有各功能根目录（`bili_videos/`、`meme_library/meme_db.json` 等），JSON 读写用 `json.dump(..., ensure_ascii=False, indent=2)`。
- `MessageEventResult.is_model_result()`：仅对 `LLM_RESULT` / `AGENT_RUNNER_ERROR` 返回 True；流式结果（`STREAMING_RESULT` 不会触发钩子、`STREAMING_FINISH` 不满足 `is_model_result()`）会被自然排除。因此用它作为"只处理 LLM 回复"的守卫最合适。
- 发送单独消息使用 `self.context.send_message(umo, MessageChain(...))`（现有 `_send_meme_separately` 已采用）。
- AstrBot 自带的 `segmented_reply` 按标点/正则拆分，不处理纯文本内换行，且受全局配置开关影响——这正是用户需要插件级新解析器的原因。

---

## 三、任务一：换行分段解析器

### 3.1 新增配置（`_conf_schema.json`）
新增 `segmented_parser_config` 对象：

```json
"segmented_parser_config": {
  "describe": "LLM回复换行分段解析器配置",
  "type": "object",
  "items": {
    "available": { "describe": "是否启用换行分段解析器", "type": "bool", "default": false },
    "send_interval": { "describe": "分段消息之间的发送间隔（秒）", "type": "float", "default": 0.5 }
  }
}
```

### 3.2 新增钩子（[main.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/main.py)）
在现有 `on_decorating_result_hook`（315 行）**之后**新增 `@filter.on_decorating_result()` 方法 `on_decorating_result_split_hook`：

**为什么放在之后**：保证先经过表情包钩子的占位符清理，再对清理后的文本做拆分。

**逻辑**（`async def`，内部全部 try/except + logger）：
1. 读取 `segmented_parser_config`，未启用则直接返回。
2. `result = event.get_result()`；为 None 或 `not result.is_model_result()` 则返回（只处理 LLM 回复，命令输出等普通结果不拆分）。
3. 遍历 `result.chain`：
   - `Plain` 组件：`text.split("\n")` 逐行 `line.strip()`，**跳过空/纯空白行**（用户明确要求不发空白信息），非空行收集为 `plain_segments`。
   - 非 `Plain` 组件（如 Image 等）：收集到 `other_comps`，各自作为独立消息发送。
4. 若 `len(plain_segments) + len(other_comps) <= 1`：无需拆分，直接返回（走原有流程）。
5. 发送：
   ```
   umo = event.unified_msg_origin
   first = True
   for comp in other_comps:            # 非文本组件先各自发送
       if not first: await asyncio.sleep(send_interval)
       await self.context.send_message(umo, MessageChain(chain=[comp]))
       first = False
   for line in plain_segments:         # 每个非空行一条消息
       if not first: await asyncio.sleep(send_interval)
       await self.context.send_message(umo, MessageChain(chain=[Plain(line)]))
       first = False
   ```
6. 全部发送完成后 `event.clear_result()`，防止管道重复发送原合并消息。

**关键点**：
- 只对 LLM 结果生效（`is_model_result()`），普通指令输出（如推特订阅列表）不会被拆碎。
- 拆分行已 `.strip()`，消息间用 `send_interval` 间隔保证顺序与限速。
- 流式输出不适用（与表情包系统相同的前提条件）。

---

## 四、任务二：情绪与好感度系统

### 4.1 新增配置（`_conf_schema.json`）
新增 `emotion_config` 对象（`llm_provider_id` 复用 `_special: select_provider` 模式）：

```json
"emotion_config": {
  "describe": "情绪与好感度系统配置",
  "type": "object",
  "items": {
    "available": { "describe": "是否启用情绪与好感度系统", "type": "bool", "default": false },
    "llm_provider_id": {
      "describe": "用于情绪/好感度分析的LLM Provider（高温度）",
      "type": "string", "default": "",
      "_special": "select_provider",
      "hint": "留空时使用第一个可用的模型"
    },
    "temperature": { "describe": "情绪分析模型的温度（高温度设置）", "type": "float", "default": 1.2 },
    "affinity_init": { "describe": "新用户好感度初始值", "type": "int", "default": 50 },
    "affinity_min": { "describe": "好感度下限", "type": "int", "default": 0 },
    "affinity_max": { "describe": "好感度上限", "type": "int", "default": 100 },
    "delta_max": { "describe": "单次好感度变化值绝对值上限", "type": "int", "default": 5 }
  }
}
```

### 4.2 提示词（[core/prompts.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/core/prompts.py)）
新增：

- `EMOTION_ANALYSIS_SYSTEM_PROMPT = "你是聊天机器人的情绪/好感度评估，负责为LLM建立情绪系统，根据用户发送给你的内容决定你应该如何变化你的情绪和对对应用户的好感度变化。只返回 JSON，不要返回其他内容。"`
- `EMOTION_ANALYSIS_PROMPT` 模板 + `format_emotion_analysis_prompt(nickname, message)` 函数：要求模型返回 `{"emotion_delta": 整数, "affinity_delta": 整数}`，说明模型对用户的好感度变化和自身的情绪。
- `EMOTION_STATE_INJECTION` 模板 + `format_emotion_state_injection(nickname, emotion, affinity)` 函数：生成注入主 LLM system_prompt 的"当前用户状态"段落（昵称/对他的好感度，并提示按好感度调整语气），同时增加情绪段落，按照情绪调整回复态度。

### 4.3 新增模块（新文件 `core/api/emotion_apis.py`）
仿照 [meme_apis.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/core/api/meme_apis.py)：

```python
async def analyze_user_emotion(
    context: Context,
    provider_id: str,
    nickname: str,
    message: str,
    temperature: float,
) -> Optional[dict]:
```
- 获取 Provider：`context.get_provider_by_id(provider_id)`，None 则退回 `context.get_all_providers()[0]`，无可用 Provider 返回 None。
- `await provider.text_chat(prompt=format_emotion_analysis_prompt(...), system_prompt=EMOTION_ANALYSIS_SYSTEM_PROMPT, temperature=temperature)`。
- 解析 JSON：剥离 `<|...|>` 标记、处理 markdown 代码块（复用 meme_apis 的健壮解析写法），校验 `emotion` 为非空字符串、`affinity_delta` 转 int 并截断到 `[-delta_max, delta_max]`。
- 返回 `{"emotion": ..., "affinity_delta": ...}`；失败时 logger 警告并返回 None。

### 4.4 持久化（[core/api/storage_apis.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/core/api/storage_apis.py)）
在 `DataManager` 中新增：
- `__init__`：`self.affinity_root = self.root / 'affinity'`；`create_folder` 中创建该目录。
- `_get_affinity_file()` → `affinity_root / 'affinity_db.json'`。
- `_load_affinity_db()` / `_save_affinity_db(db)`（读写模式与现有 JSON 一致）。
- `get_user_affinity(user_id, nickname="") -> int`：**不存在则初始化为 50 并持久化**（满足"新出现的用户从 50 开始"），存在则返回；顺便更新昵称。
- `update_user_affinity(user_id, delta, nickname="", min_=50默认下限, max_=上限) -> int`：累加并 clamp 到 `[affinity_min, affinity_max]`，持久化，返回新值。
- 数据格式：`{user_id: {"affinity": int, "nickname": str, "last_emotion": str, "last_update": str}}`。

### 4.5 主流程接入（[main.py](file:///f:/astrbot/AstrBot/data/plugins/astrbot_plugin_Fiscok/main.py)）

**A. 新用户好感度初始化钩子** `@filter.event_message_type(filter.EventMessageType.ALL)` → `ensure_user_affinity_on_message`：
- 未启用 `emotion_config.available` 则返回。
- `sender_id` 为空或等于 `event.get_self_id()`（机器人自身）则跳过。
- 调用 `self.data_manager.get_user_affinity(sender_id, sender_name)`，保证任何新出现的用户第一时间获得初始值 50。

**B. 情绪分析 + 注入钩子** `@filter.on_llm_request()` → `on_llm_request_emotion_hook`：
- 未启用则返回；`sender_id` 为空或为机器人自身则返回。
- 阻塞式调用 `analyze_user_emotion(self.context, provider_id, nickname, event.message_str, temperature)`（仅在回复时调用，符合用户选择）。
- 成功：`new_affinity = self.data_manager.update_user_affinity(...)`（含 delta 截断与 clamp），然后 `req.system_prompt = f"{req.system_prompt or ''}\n{format_emotion_state_injection(nickname, emotion, new_affinity)}"`。
- 失败/异常：logger 警告后返回，**绝不阻塞或中断主 LLM 请求**（整体 try/except）。

**C. 顶部导入**：`from .core.api.emotion_apis import analyze_user_emotion`，`from .core.prompts import format_emotion_analysis_prompt, format_emotion_state_injection, EMOTION_ANALYSIS_SYSTEM_PROMPT`。

---

## 五、修改文件清单

| 文件 | 改动 |
|------|------|
| `_conf_schema.json` | 新增 `segmented_parser_config`、`emotion_config` 两个配置对象 |
| `core/prompts.py` | 新增情绪分析/注入相关的 2 个模板 + 2 个格式化函数 |
| `core/api/emotion_apis.py` | **新建**，`analyze_user_emotion` 高温度模型调用 |
| `core/api/storage_apis.py` | `DataManager` 新增好感度 JSON 持久化（初始化 50 / 增减 / clamp） |
| `main.py` | 新增 3 个钩子（换行分段解析、新用户初始化、情绪分析与注入）+ 导入 |

---

## 六、Assumptions & Decisions

1. **分段解析器只作用于 LLM 结果**（`is_model_result()`），不拆分指令输出等普通消息，避免订阅列表等文本被拆碎。
2. **按每个换行拆分**（用户已确认），每行 `strip()` 后发送，空行/纯空白行丢弃（用户已确认"不要发送空白信息"）。
3. **流式输出不适用**：与表情包系统相同的前提（`on_decorating_result` 钩子在流式下不生效）。
4. **情绪模型仅在回复时调用**（用户已确认），在 `on_llm_request` 中阻塞式 `await`，分析失败不阻断主请求。
5. **分析结果注入主 LLM system_prompt**（用户已确认），好感度实时更新后注入。
6. **模型获取方式与表情包系统一致**：`context.get_provider_by_id` → `context.get_all_providers()[0]` 兜底；`temperature` 通过 `text_chat(**kwargs)` 透传。
7. **好感度**：初始 50，范围 `[affinity_min, affinity_max]`（默认 0~100），单次变化截断在 `[-delta_max, delta_max]`（默认 ±5），JSON 持久化于 `plugin_data/Fiscok-s Plugins/affinity/affinity_db.json`。
8. **钩子顺序**：换行分段钩子定义在表情包 `on_decorating_result_hook` 之后，确保先清理占位符再拆分。

---

## 七、Verification Steps

1. **配置加载**：在 WebUI 重新加载插件后检查 `_conf_schema.json` 新配置项出现。
2. **分段解析器**：
   - 启用 `segmented_parser_config.available`，让 LLM 返回含多行/多段文本的回复，确认每条非空行被单独发送、无空白消息、顺序正确。
   - 执行普通指令（如 `推特管理 订阅列表`），确认多行输出**不被**拆分。
3. **好感度初始化**：启用 `emotion_config.available`，新用户发消息后检查 `affinity/affinity_db.json` 出现该用户且 affinity=50。
4. **情绪分析**：观察日志出现情绪分析调用；对友好/嘲讽消息分别验证好感度变化方向；`affinity_db.json` 中值在 0~100 内。
5. **上下文注入**：检查主 LLM 请求的 system_prompt 包含"当前用户状态"段落（昵称/情绪/好感度）。
6. **稳定性**：情绪模型调用失败（如 Provider 不可用）时，确认主 LLM 回复不受影响。

## 八、请帮助我明确向LLM发送信息时携带的用户信息是否包含QQ账号，如果是，请依照QQ账号作为锁定用户的KEY，否则请先向我说明，我需要预防错误的附带了好感度信息（因为群昵称/昵称变化导致）

## 九、注意，我仅修改了情绪/好感度描述部分，这部分和你之前的认知有较大偏差，具体实行细节我修改的不多，如有遗漏请按照我的修改为主去修改我未改动的部分。再次强调：**好感度/情绪是模型对于用户的情感态度，是对模型的拟人化处理**