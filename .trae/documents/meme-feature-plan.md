# 表情包功能实现计划

## 一、功能概述

实现表情包的自动学习（从群聊消息中随机保存）和智能发送（在 LLM 回复中插入占位符，解析后发送图片）。

---

## 二、数据流设计

### 表情包入库流程
```
群消息 → 检测表情包(sub_type==1) → 概率判定(emoji_learn_positive)
  → 下载表情包图片到本地 → 调用 LLM 生成描述标签 → 保存元数据到 meme_db.json
```

### 表情包发送流程
```
LLM请求 → 概率判定(emoji_attach_positive) → 注入system_prompt引导生成占位符
  → LLM响应 → 解析占位符 → 匹配表情包 → 追加Image组件到消息链
```

---

## 三、存储结构设计

遵循现有模式：`根目录/特定功能文件夹`

```
plugin_data/Fiscok-s Plugins/
├── meme_library/                    # 新增：表情库目录
│   ├── meme_db.json                 # 元数据库（描述、标签、情绪分类）
│   ├── {image_id}.jpg               # 表情包图片文件
│   └── ...
├── bili_videos/
├── twitter_cache/
└── ...
```

### meme_db.json 结构
```json
[
  {
    "id": "meme_20260702_001",
    "filename": "meme_20260702_001.jpg",
    "description": "一只猫咪露出惊讶的表情，眼睛瞪得很大",
    "tags": ["惊讶", "猫咪", "搞笑"],
    "emotion": "surprise",
    "source": "group_123456",
    "timestamp": "2026-07-02T12:00:00"
  }
]
```

---

## 四、配置项设计

在 `_conf_schema.json` 的 `meme_config` 中补充必要配置：

```json
{
  "meme_config": {
    "items": {
      "emoji_learn_positive": { "default": 0.1 },
      "emoji_attach_positive": { "default": 0.7 },
      "meme_available": {
        "describe": "是否启用表情包功能",
        "type": "bool",
        "default": false
      },
      "llm_provider_id": {
        "describe": "用于表情包描述生成的LLM Provider ID（留空则使用默认）",
        "type": "string",
        "default": ""
      },
      "placeholder_tag": {
        "describe": "LLM回复中表情包占位符的标签名",
        "type": "string",
        "default": "meme"
      }
    }
  }
}
```

---

## 五、修改文件清单

### 1. `_conf_schema.json` — 添加配置项
- 添加 `meme_available` 开关
- 添加 `llm_provider_id`（用于描述生成的 LLM）
- 添加 `placeholder_tag`（占位符标签名）

### 2. `core/api/storage_apis.py` — 添加表情库存储管理

新增属性和方法：

```python
# __init__ 中新增
self.meme_library_root = self.root / 'meme_library'

# create_folder 中新增目录创建
self.meme_library_root.mkdir(parents=True, exist_ok=True)

# 新增方法
def _load_meme_db(self) -> List[Dict]
def _save_meme_db(self, db: List[Dict])
def add_meme(self, image_path: str, description: str, tags: List[str], emotion: str, source: str) -> str
def find_meme_by_emotion(self, emotion: str) -> Dict | None
def find_meme_random(self) -> Dict | None
def get_meme_by_id(self, meme_id: str) -> Dict | None
```

### 3. `main.py` — 核心逻辑修改

#### 3.1 重命名 `handle_llm_request` → `on_llm_request_hook`
- 在 `on_llm_request` 钩子中：
  - 检测消息中的表情包
  - 概率判定是否保存（`emoji_learn_positive`）
  - 若保存：下载图片 → 调用 LLM 生成描述 → 入库

#### 3.2 新增 `on_llm_response_hook` 方法
- 使用 `@filter.on_llm_response()` 装饰器
- 概率判定是否注入占位符（`emoji_attach_positive`）
- 实际上占位符注入需要在 request 阶段完成（通过 system_prompt），response 阶段负责解析占位符并替换为图片

#### 3.3 新增 `on_decorating_result_hook` 方法
- 使用 `@filter.on_decorating_result()` 装饰器
- 解析 LLM 回复中的占位符 `[meme:xxx]`
- 根据描述匹配表情库中的图片
- 将 `Image` 组件追加到消息链

#### 3.4 注入 system_prompt 的逻辑
在 `on_llm_request` 中，根据概率往 `req.system_prompt` 追加：
```
如果需要使用表情包来增强回复效果，可以在回复中使用占位符 [meme:情绪描述]，
例如 [meme:开心]、[meme:无语]、[meme:困惑]。
可用的情绪关键词：开心、难过、惊讶、无语、愤怒、困惑、害羞、搞笑。
```

### 4. 新增文件：`core/api/meme_apis.py` — LLM 描述生成

```python
async def generate_meme_description(image_url: str, provider_id: str = "") -> Dict:
    """
    调用 LLM 对表情包图片进行描述和标签生成

    Args:
        image_url: 图片本地路径或URL
        provider_id: LLM Provider ID

    Returns:
        {"description": "...", "tags": [...], "emotion": "..."}
    """
```

**描述生成提示词模板**：
```
请分析这张表情包图片，返回以下JSON格式：
{
  "description": "简短描述图片内容（20字以内）",
  "tags": ["标签1", "标签2", "标签3"],
  "emotion": "情绪关键词（从以下选择：happy/sad/surprised/angry/confused/shy/funny/speechless）"
}
只返回JSON，不要返回其他内容。
```

---

## 六、关键实现细节

### 6.1 LLM 描述生成调用方式
利用 AstrBot 的 Provider 机制，通过 `self.context` 获取已配置的 LLM Provider：

```python
# 获取 Provider 实例
provider = self.context.get_provider_by_id(provider_id)
# 或使用默认 Provider
provider = self.context.get_default_provider()

# 构造请求并获取响应
response = await provider.text_chat(prompt="...", image_urls=[image_path])
```

### 6.2 占位符格式
- 注入格式：`[meme:情绪关键词]`
- 示例：`[meme:开心]`、`[meme:无语]`
- 解析正则：`\[meme:(.+?)\]`

### 6.3 表情包匹配逻辑
1. 精确匹配：`emotion` 字段与占位符内容匹配
2. 标签匹配：`tags` 列表中包含占位符关键词
3. 随机回退：无匹配时随机选择一张

### 6.4 图片发送
使用 `Image.fromFileSystem()` 从本地路径构建图片组件，追加到消息链。

---

## 七、实现步骤

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1 | `_conf_schema.json` | 添加表情包相关配置项 |
| 2 | `core/api/storage_apis.py` | 添加 `meme_library_root` 属性和表情库 CRUD 方法 |
| 3 | `core/api/meme_apis.py` | 新建文件，实现 LLM 描述生成函数 |
| 4 | `main.py` | 重命名钩子方法、实现表情包入库逻辑 |
| 5 | `main.py` | 实现 system_prompt 注入逻辑 |
| 6 | `main.py` | 实现 `on_decorating_result` 占位符解析和图片追加 |

---

## 八、Assumptions & Decisions

1. **LLM Provider 选择**：复用 AstrBot 已配置的 Provider，而非单独配置 API
2. **占位符格式**：使用 `[meme:xxx]` 而非其他格式，简洁且不易与正常文本冲突
3. **表情包分类**：采用情绪分类（8类），简单高效，便于 LLM 理解
4. **不修改现有框架**：仅在现有钩子和存储模式基础上扩展
5. **图片存储**：直接保存为文件，使用唯一 ID 命名，元数据集中存储在 JSON

---

## 九、Verification Steps

1. 测试表情包检测：发送含表情包的消息，检查日志是否正确识别
2. 测试概率触发：多次发送消息，验证 `emoji_learn_positive` 概率生效
3. 测试 LLM 描述生成：检查 `meme_db.json` 中是否正确写入描述和标签
4. 测试占位符注入：检查 LLM 请求的 system_prompt 是否包含占位符说明
5. 测试占位符解析：验证 LLM 回复中的占位符被正确替换为图片
6. 测试图片发送：确认消息链中包含 Image 组件且图片能正常显示
