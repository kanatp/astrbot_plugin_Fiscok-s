# AstrBot 伴侣型插件 (astrbot_plugin_Fiscok) 全功能手册、指令清单与数据持久化结构

本文档系统性说明 `astrbot_plugin_Fiscok` 插件的**全部功能模块**、**完整指令清单**（包括 Twitter/Instagram 订阅、Cookie 管理等所有功能）以及**所有持久化文件的存储路径与 JSON 结构规范**，供开发者、管理员进行运维、备份与二次开发查阅。

---

## 目录
- [一、 核心功能体系](#一-核心功能体系)
- [二、 完整指令清单（按模块分类）](#二-完整指令清单按模块分类)
  - [1. 用户身份与固定昵称锚定 (`/用户昵称`)](#1-用户身份与固定昵称锚定-用户昵称)
  - [2. 待办约定与情感关怀 (`/关怀`)](#2-待办约定与情感关怀-关怀)
  - [3. 话题自动行进连续任务 (`/行进状态`, `/停止任务`)](#3-话题自动行进连续任务-行进状态-停止任务)
  - [4. CLM 自主上下文管理 (`/上下文`)](#4-clm-自主上下文管理-上下文)
  - [5. 机器人虚拟日常生活 (`/日常`)](#5-机器人虚拟日常生活-日常)
  - [6. Twitter 订阅与推送管理 (`/推特管理`)](#6-twitter-订阅与推送管理-推特管理)
  - [7. Instagram 订阅与推送管理 (`/ins管理`)](#7-instagram-订阅与推送管理-ins管理)
  - [8. 话题记忆与私聊个人记忆 (`/话题记忆管理`, `/个人记忆`)](#8-话题记忆与私聊个人记忆-话题记忆管理-个人记忆)
  - [9. 人设自我演化管理 (`/人设管理`)](#9-人设自我演化管理-人设管理)
  - [10. 图库管理与底层调试指令 (`/图库管理`, `/拉取测试`, `/推送测试`)](#10-图库管理与底层调试指令-图库管理-拉取测试-推送测试)
- [三、 数据持久化结构规范（全部文件与 JSON Schema）](#三-数据持久化结构规范全部文件与-json-schema)
  - [1. 目录结构概览](#1-目录结构概览)
  - [2. 用户身份与固定昵称表 (`user_identities.json`)](#2-用户身份与固定昵称表-user_identitiesjson)
  - [3. 待办约定与情感关怀库 (`commitments.json`)](#3-待办约定与情感关怀库-commitmentsjson)
  - [4. CLM 工作记忆账本 (`context_files/{safe_umo}.json`)](#4-clm-工作记忆账本-context_filessafe_umojson)
  - [5. 机器人自主日常生活日程 (`bot_daily_life.json`)](#5-机器人自主日常生活日程-bot_daily_lifejson)
  - [6. 主动聊天状态与作息画像 (`proactive_states.json`)](#6-主动聊天状态与作息画像-proactive_statesjson)
  - [7. Twitter 订阅记录与推送索引 (`twitter_subscriptions.json`, `unified_msg_origin_record.json`, `twitter_push_record.json`, `twitter_cache/`)](#7-twitter-订阅记录与推送索引)
  - [8. Instagram 订阅记录与推送索引 (`instagram_subscriptions.json`, `instagram_umo_record.json`, `instagram_push_record.json`, `instagram_cache/`)](#8-instagram-订阅记录与推送索引)
  - [9. 统一网络凭证文件 (`cookies.json`)](#9-统一网络凭证文件-cookiesjson)
  - [10. B 站视频分享重复统计 (`group_data/{group_id}.json`)](#10-b-站视频分享重复统计-group_datagroup_idjson)
  - [11. 情绪好感度数据库 (`affinity_root/affinity_db.json`)](#11-情绪好感度数据库-affinity_rootaffinity_dbjson)
  - [12. 话题记忆数据库 (`topic_memory_root/topic_db.json`)](#12-话题记忆数据库-topic_memory_roottopic_dbjson)
  - [13. 私聊个人记忆库 (`personal_memory_root/personal_memories/{user_id}.json`)](#13-私聊个人记忆库-personal_memory_rootpersonal_memoriesuser_idjson)
  - [14. 演化自我设定 (`evolved_persona/evolved_persona.json`)](#14-演化自我设定-evolved_personaevolved_personajson)
  - [15. 表情包记录数据库 (`meme_data/meme_records.json`)](#15-表情包记录数据库-meme_datameme_recordsjson)
  - [16. WebUI 提示词库持久化 (`prompt_library.json`)](#16-webui-提示词库持久化-prompt_libraryjson)

---

## 一、 核心功能体系

插件采用解耦的**服务层架构（`core/services/`）**与模块化设计，具备以下核心能力：

1. **认知防漂移与消息合并**：
   * **`NicknameAnchorService`**：用户 ID 与首次登记昵称不可变锚定，防止群名片频繁修改导致模型认知混乱；
   * **`MergeService`**：高优先级（`priority=sys.maxsize - 10`）防抖合并连续短消息，按 `(umo, sender_id)` 隔离避免群聊串台，同步重构 Plain 组件链。
2. **情感羁绊与拟人互动**：
   * **`EmotionService`**：会话级独立情绪隔离，情绪强度（1~5）与后台每 15 分钟心跳自然半衰衰减（45 分钟无刺激平滑回落“平静”）；
   * **好感度四阶多维羁绊**：初识（0-39）、熟悉（40-69）、互信（70-89）、知心（90-100），高好感度下解锁双向情感倾诉与撒娇微情绪；
   * **`TypingService`**：拟真打字延迟算法（短句快回、长句稍候），协议端模拟“正在输入中...”。
3. **多端在场与时空陪伴**：
   * **`PresenceService`**：Live2D 桌面端在场感知（PC 在场时自动抑制手机 QQ 私聊的主动搭话）；
   * **`CommitmentTrackerService`**：智能推断时间与健康/情绪/日程分类，到期在对话与主动搭话中温和回响（“答辩怎么样啦/感冒好些了吗”）；
   * **`BotLifeEngine`**：每夜凌晨自主调用模型规划新一天 24 小时 8 个时段的生活日程与主题，支持平滑 Fallback 兜底池；
   * **主动生活随拍分享**：私聊主动聊天时，模型可结合生活碎念自主附带生活照占位符 `[pic:图片ID]`，分步优雅发送。
4. **自主连续推进与 CLM 上下文管理**：
   * **`TopicProgressionService`**：多步长任务自主推进（`[CONTINUE: 下一步]` / `[DONE]`），等待期间拦截用户插话并重置倒计时（“先解答、后推进”），支持终止意图安全打断；
   * **`CLMContextService`**：基于 Meta FAIR 顶会论文《Context Language Models》，接替原生滑窗，实现 **Context as a File (CaaF)** 结构化工作记忆账本，直传上下文精简为最新 6 条紧凑记录（Token 降低 60%+），后台自主执行锚定、更新、浓缩与剪枝。
5. **多平台搬运与社交媒体订阅推送**：
   * **Twitter 订阅推送**：基于 RSSHub 定时批量拉取推特博主动态，以合并转发消息（Forward Node）形式推送到指定群聊，支持图片、视频预览及 Cookie 维护；
   * **Instagram 订阅推送**：基于 Instaloader 定时抓取指定博主的帖子与快拍（Stories），带图文合并转发推送；
   * **Bilibili 重复视频统计（火星救援）**：自动检测群内分享的 B 站视频 BV 号，提示首次分享者、时间与历史累计重复次数。
6. **双端协作与 WebUI 面板**：
   * Live2D 桌面端桥接（`core/suzu_bridge.py`）与浏览器管理面板（`core/suzu_webui.py`），提供全量配置、状态监控、提示词在线编辑、记忆与账本在线管理。

---

## 二、 完整指令清单（按模块分类）

所有指令均支持斜杠前缀（如 `/` 或 `／`）。

### 1. 用户身份与固定昵称锚定 (`/用户昵称`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/用户昵称 show` | `/用户昵称 查看` | 所有人 | `[QQ号]` (留空默认为发送者) | 查看指定用户或自己的固定锚定昵称、最新群名片、好感度、羁绊阶段、作息习惯画像与登记时间 |
| `/用户昵称 set` | `/用户昵称 修改` | 管理员 | `<QQ号> <新固定昵称>` | 手动修改指定用户的固定锚定昵称，即刻生效并持久化 |
| `/用户昵称 reset` | `/用户昵称 重置` | 管理员 | `<QQ号>` | 清除指定用户的身份记录，该用户下次发言时将重新以当时昵称登记 |

---

### 2. 待办约定与情感关怀 (`/关怀`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/关怀 show` | `/关怀 查看` | 所有人 | `[QQ号]` (留空默认为发送者) | 查看名下所有关怀事项、预计回响时间及状态（待关怀 / 已回响 / 已过期） |
| `/关怀 add` | `/关怀 添加` | 所有人 | `<事项内容>` | 手动添加一条关怀约定（如：`/关怀 添加 明天下午有答辩`，自动推断时间与类型） |
| `/关怀 delete` | `/关怀 删除` | 所有人 | `<事项ID>` | 删除指定的关怀事项备忘 |
| `/关怀 clear` | `/关怀 清空` | 所有人 | 无 | 清空当前用户名下的全部关怀约定记录 |

---

### 3. 话题自动行进连续任务 (`/行进状态`, `/停止任务`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/progression_status` | `/行进状态`, `/任务状态` | 所有人 | 无 | 查看当前会话是否处于持续任务中、目标、进行轮数（已进行轮数 / 上限）、等待状态与待合并插话数 |
| `/progression_stop` | `/停止任务`, `/结束任务` | 所有人 | 无 | 强制立即终止当前会话正在运行的自动行进任务 |

---

### 4. CLM 自主上下文管理 (`/上下文`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/上下文 show` | `/上下文 查看` | 所有人 | 无 | 查看当前会话的上下文工作记忆账本全貌（核心目标、关键事实列表、历程概要、轮数与即时缓冲条数） |
| `/上下文 compact` | `/上下文 整理`, `/上下文 压缩` | 所有人 | 无 | 立即为当前会话手动触发一次 CLM 自主整理与剪枝浓缩 |
| `/上下文 reset` | `/上下文 重置`, `/上下文 清空` | 管理员 | 无 | 清空并重置当前会话的上下文工作记忆账本 |

---

### 5. 机器人虚拟日常生活 (`/日常`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/日常` | `/生活状态` | 所有人 | 无 | 查看机器人此刻正在做什么、生活氛围、今日主题与心情 |
| `/日常 列表` | `/日常 日程`, `/日常 安排` | 所有人 | 无 | 查看机器人今日 24 小时 8 个标准时段的完整日程规划与今日主题 |
| `/日常 刷新` | `/日常 重新生成`, `/日常 重新规划` | 所有人 | 无 | 立即调用模型为今日重新构想一份 24 小时生活日程 |
| `/日常 设置 <内容>` | `/日常 自定义 <内容>` | 所有人 | `<内容>` | 手动将机器人此刻状态设定为特定内容（如：`/日常 设置 正在做手工小饼干`） |
| `/日常 恢复` | `/日常 重置` | 所有人 | 无 | 清除手动设定，恢复模型自主规划的日程 |

---

### 6. Twitter 订阅与推送管理 (`/推特管理`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/推特管理 subscribe` | `/推特管理 订阅` | 所有人 | `<TwitterID> [别名]` | 为当前群聊添加一个 Twitter 博主订阅（Twitter ID 不含 @） |
| `/推特管理 unsubscribe` | `/推特管理 取消订阅` | 所有人 | `<TwitterID>` | 取消当前群聊对指定 Twitter 账号的订阅 |
| `/推特管理 list` | `/推特管理 订阅列表` | 所有人 | 无 | 查看当前群聊已订阅的 Twitter 账号列表及别名 |
| `/推特管理 update_cookie` | `/推特管理 更新Cookie` | 管理员 | `<auth_token> <ct0>` | 更新 RSSHub 容器的 Twitter Cookie 并自动重启 RSSHub 容器，同步写入 `cookies.json` |
| `/推特管理 check_available`| `/推特管理 检查连接状态` | 管理员 | 无 | 检查 RSSHub 服务网络连接与可用性状态 |
| `/推特管理 trigger_cache_update` | `/推特管理 手动缓存更新` | 管理员 | 无 | 手动立即触发一次已订阅 Twitter 账号的动态抓取与缓存更新 |
| `/推特管理 trigger_scheduled_push` | `/推特管理 手动推送` | 管理员 | 无 | 手动立即触发一次向各群聊推送 Twitter 缓存动态 |

---

### 7. Instagram 订阅与推送管理 (`/ins管理`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/ins管理 subscribe` | `/ins管理 订阅` | 所有人 | `<Instagram用户名> [别名]`| 为当前群聊添加一个 Instagram 账号订阅 |
| `/ins管理 unsubscribe` | `/ins管理 取消订阅` | 所有人 | `<Instagram用户名>` | 取消当前群聊对指定 Instagram 账号的订阅 |
| `/ins管理 list` | `/ins管理 订阅列表` | 所有人 | 无 | 查看当前群聊已订阅的 Instagram 账号列表及别名 |
| `/ins管理 check_cookies` | `/ins管理 检查cookies` | 管理员 | 无 | 检查当前 Instagram cookies 是否有效（能否正常登录） |
| `/ins管理 reload_cookies`| `/ins管理 重载cookies` | 管理员 | 无 | 从本地 `cookies.json` 重新加载 Instagram 登录凭证并重建加载器 |
| `/ins管理 update_cookies`| `/ins管理 更新cookies` | 管理员 | `<sessionid> <ds_user_id> <csrftoken>` | 更新 Instagram cookies 并持久化保存到 `cookies.json` |
| `/ins管理 trigger_cache_update` | `/ins管理 手动缓存更新` | 管理员 | 无 | 手动立即触发一次已订阅 Instagram 账号的帖子与快拍抓取更新 |
| `/ins管理 trigger_push` | `/ins管理 手动推送` | 管理员 | 无 | 手动立即触发一次向各群聊推送 Instagram 缓存动态 |

---

### 8. 话题记忆与私聊个人记忆 (`/话题记忆管理`, `/个人记忆`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/话题记忆管理 状态` | 无 | 所有人 | 无 | 查看当前会话已记录的话题记忆条数与上限配置 |
| `/话题记忆管理 列表` | 无 | 所有人 | `[数量]` (默认 10) | 查看当前会话最近记录的话题记忆文本与归属发言人 QQ |
| `/话题记忆管理 统计` | 无 | 所有人 | 无 | 查看全局所有会话的话题记忆库总条数 |
| `/话题记忆管理 清空` | 无 | 管理员 | 无 | 清空当前会话的全部话题记忆 |
| `/个人记忆 状态` | 无 | 所有人 | 无 | 查看当前私聊用户的个人长期记忆条数、各分类统计与容量上限 |
| `/个人记忆 列表` | 无 | 所有人 | `[分类]` (如 profile/preference/plan/note/social/taboo) | 查看当前私聊用户的长期记忆清单与重要度 |
| `/个人记忆 清空` | 无 | 管理员 | 无 | 清空当前用户的个人长期记忆库 |

---

### 9. 人设自我演化管理 (`/人设管理`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/人设管理 查看` | 无 | 所有人 | 无 | 查看当前演化出的“自我设定”文本与最近一次演化修订原因 |
| `/人设管理 演化一次` | 无 | 所有人 | 无 | 基于近期会话素材手动触发一次模型自主修订自我设定判定 |
| `/人设管理 重置` | 无 | 管理员 | 无 | 清空自我设定，恢复为只使用 AstrBot 原生人设 |

---

### 10. 图库管理与底层调试指令 (`/图库管理`, `/拉取测试`, `/推送测试`)
| 指令 | 别名 | 权限 | 参数 | 功能说明 |
| :--- | :--- | :--- | :--- | :--- |
| `/图库管理 统计` | 无 | 所有人 | 无 | 查看当前已学习表情包总数与人设立绘图库文件数 |
| `/pull_cache_test` | `/拉取测试` | 管理员 | 无 | 调试指令：测试抓取指定博主（默认 aimi_sound）缓存 |
| `/push_cache_test` | `/推送测试` | 管理员 | 无 | 调试指令：测试向群聊推送转发缓存消息 |

---

## 三、 数据持久化结构规范（全部文件与 JSON Schema）

所有数据文件统一存放在 AstrBot 数据目录下的本插件独立数据夹：
`data/plugin_data/astrbot_plugin_Fiscok/`

### 1. 目录结构概览
```text
data/plugin_data/astrbot_plugin_Fiscok/
├── cookies.json                              # Twitter 与 Instagram 统一凭证
├── user_identities.json                      # 用户 ID - 首次登记固定昵称不可变锚定表
├── commitments.json                          # 待办约定与情感关怀追踪库
├── bot_daily_life.json                       # 模型自主规划的 24 小时日常生活日程
├── proactive_states.json                     # 主动发言冷却与用户作息时段统计画像
├── twitter_subscriptions.json                # Twitter 订阅列表记录
├── unified_msg_origin_record.json            # Twitter 群号 -> UMO 映射
├── twitter_push_record.json                  # Twitter 已推送推文 ID 记录
├── instagram_subscriptions.json              # Instagram 订阅列表记录
├── instagram_umo_record.json                 # Instagram 群号 -> UMO 映射
├── instagram_push_record.json                # Instagram 已推送帖子 ID 记录
├── prompt_library.json                       # WebUI 在线统一维护的提示词库落盘文件
├── context_files/                            # CLM 工作记忆账本 (每会话独立)
│   └── {safe_umo}.json
├── group_data/                               # B 站视频分享去重历史 (每群独立)
│   └── {group_id}.json
├── affinity_root/                            # 好感度数据库
│   └── affinity_db.json
├── topic_memory_root/                        # 话题记忆向量数据库
│   └── topic_db.json
├── personal_memory_root/                     # 私聊个人记忆库 (每用户独立)
│   └── personal_memories/
│       └── {user_id}.json
├── evolved_persona/                          # 人设演化叠加层
│   └── evolved_persona.json
├── meme_data/                                # 表情包学习数据库与图片
│   ├── meme_records.json
│   └── images/
├── twitter_cache/                            # Twitter 动态本地缓存 JSON
│   └── {twitter_id}.json
└── instagram_cache/                          # Instagram 动态本地缓存 JSON
    └── {username}.json
```

---

### 2. 用户身份与固定昵称表 (`user_identities.json`)
* **作用**：记录用户 ID 与第一次被插件捕捉到的昵称，后续组装 Prompt 时统一锁定为该昵称，彻底消除认知漂移。
```json
{
  "123456789": {
    "first_nickname": "鱼饼",
    "registered_at": "2026-11-20T10:00:00.000000",
    "last_seen_nickname": "鱼饼(出差中)",
    "last_seen_at": "2026-11-20T14:30:00.000000",
    "manually_set": false
  }
}
```

---

### 3. 待办约定与情感关怀库 (`commitments.json`)
* **作用**：记录用户提到的时效性事件、身心健康与情绪要点，到期后在对话或主动搭话中精准回响。
```json
{
  "123456789": [
    {
      "id": "care_1795150000_123",
      "topic": "明天下午有毕业论文答辩",
      "target_time": 1795240000.0,
      "concern_type": "event",
      "follow_up_hint": "询问事情进展如何、辛苦了一天适当放松",
      "status": "pending",
      "created_at": "2026-11-19T20:00:00.000000",
      "fulfilled_at": null
    }
  ]
}
```
* **字段说明**：
  * `concern_type`：`event`（日程事件）、`health`（身心健康）、`mood`（情绪心境）；
  * `status`：`pending`（待关怀）、`fulfilled`（已问候闭环）、`expired`（超时 48 小时过期）。

---

### 4. CLM 工作记忆账本 (`context_files/{safe_umo}.json`)
* **作用**：每会话独立的 Context as a File 工作记忆账本，接替 AstrBot 原生线性历史。
```json
{
  "umo": "aiocqhttp:group_message:123456",
  "pinned_anchors": [
    "当前核心任务：为下周五的开源项目发布会做方案策划",
    "用户的核心约束：方案预算控制在 5000 元以内，风格要科技极简"
  ],
  "distilled_facts": [
    "已确定的时间节点：2026-11-27 下午两点开场",
    "场地已选定在科技园A座路演厅"
  ],
  "milestone_summary": "前期讨论了发布会的整体定位，排除了昂贵的外部公关方案，确定由内部团队自主承办并制作宣传片。",
  "recent_buffer": [
    {"role": "user", "content": "宣传片的配乐我们用什么风格比较好？"},
    {"role": "assistant", "content": "建议选用轻快有节奏感的电子轻音乐。"}
  ],
  "turn_count": 3,
  "last_compacted_at": "2026-11-20T12:00:00.000000"
}
```

---

### 5. 机器人自主日常生活日程 (`bot_daily_life.json`)
* **作用**：每夜由模型自主结合自然日期、星期、节日、人设与近期话题规划的 24 小时日程。
```json
{
  "date": "2026-11-20",
  "day_theme": "周五期待周末假期的微风午后",
  "custom_override": null,
  "schedule": [
    {"start": 0, "end": 6, "period": "深夜/凌晨", "activity": "缩在暖烘烘的被窝里听白噪音，眼睛快要睁不开了"},
    {"start": 6, "end": 9, "period": "清晨", "activity": "刚揉揉眼睛爬起来，伸着懒腰看窗外晨光，在窗边喝温水"},
    {"start": 9, "end": 12, "period": "上午", "activity": "坐在窗边认真整理笔记和桌面文件，放着轻快的纯音乐"},
    {"start": 12, "end": 14, "period": "午间", "activity": "刚刚吃饱午饭，正满足地靠在椅背上揉肚子打瞌睡"},
    {"start": 14, "end": 17, "period": "下午", "activity": "午后整理房间的小摆件，喝着柠檬红茶期待傍晚的落日"},
    {"start": 17, "end": 19, "period": "傍晚", "activity": "趴在阳台栏杆上看天边绚烂的晚霞与落日，吹着凉爽的微风"},
    {"start": 19, "end": 22, "period": "晚间", "activity": "在自己的小窝里听喜欢的歌，随手涂鸦画画，心情格外宁静"},
    {"start": 22, "end": 24, "period": "深夜", "activity": "换好舒服的睡衣裹在被子里刷手机，准备酝酿睡意了"}
  ],
  "generated_at": "2026-11-20T03:00:00.000000"
}
```

---

### 6. 主动聊天状态与作息画像 (`proactive_states.json`)
* **作用**：记录各群聊/私聊的发言次数限额与上次发言时间，并统计各用户的作息活跃画像。
```json
{
  "states": {
    "aiocqhttp:private_message:123456789": {
      "is_private": true,
      "sender_id": "123456789",
      "nickname": "鱼饼",
      "next_try": 1795160000.0,
      "last_spoke_at": 1795140000.0,
      "daily_date": "2026-11-20",
      "daily_count": 1,
      "last_messages": ["刚才在阳台看日落，今天的晚霞好漂亮呀~"]
    }
  },
  "user_habits": {
    "123456789": {
      "night": 8,
      "day": 15,
      "early": 1
    }
  }
}
```

---

### 7. Twitter 订阅记录与推送索引

#### (1) `twitter_subscriptions.json`（订阅关系）
```json
[
  {
    "twitter_id": "aimi_sound",
    "alias": "爱美",
    "group_ids": ["123456789", "987654321"]
  }
]
```

#### (2) `unified_msg_origin_record.json`（群号到 UMO 映射）
```json
{
  "123456789": "aiocqhttp:group_message:123456789",
  "987654321": "aiocqhttp:group_message:987654321"
}
```

#### (3) `twitter_push_record.json`（已推送推文 ID 记录）
```json
{
  "aimi_sound": [
    "1795000000000000001",
    "1795000000000000002"
  ]
}
```

#### (4) `twitter_cache/{twitter_id}.json`（推文缓存内容）
```json
[
  {
    "content_id": "1795000000000000001",
    "text": "本日の生放送ありがとうございました！✨",
    "images": [
      "https://pbs.twimg.com/media/xxxxxx.jpg"
    ],
    "video": null,
    "created_at": "Fri Nov 20 18:00:00 +0000 2026"
  }
]
```

---

### 8. Instagram 订阅记录与推送索引

#### (1) `instagram_subscriptions.json`（订阅关系）
```json
[
  {
    "username": "aimi_sound",
    "alias": "爱美",
    "group_ids": ["123456789"]
  }
]
```

#### (2) `instagram_umo_record.json`（群号到 UMO 映射）
```json
{
  "123456789": "aiocqhttp:group_message:123456789"
}
```

#### (3) `instagram_push_record.json`（已推送帖子短码 Shortcode 记录）
```json
{
  "aimi_sound": [
    "CxYz123Abc",
    "CxYz456Def"
  ]
}
```

#### (4) `instagram_cache/{username}.json`（帖子缓存内容）
```json
[
  {
    "content_id": "CxYz123Abc",
    "caption": "衣装が可愛すぎました💕",
    "images": [
      "http://127.0.0.1:6199/suzu/cache/instagram/aimi_sound/CxYz123Abc_0.jpg"
    ],
    "video_url": null,
    "timestamp": 1795140000,
    "is_story": false
  }
]
```

---

### 9. 统一网络凭证文件 (`cookies.json`)
* **作用**：集中管理外部平台的鉴权 Token。
```json
{
  "twitter": {
    "auth_token": "a1b2c3d4e5f6...",
    "ct0": "0f9e8d7c6b5a..."
  },
  "instagram": {
    "sessionid": "123456789%3Axxxx%3A12",
    "ds_user_id": "123456789",
    "csrftoken": "abcdef0123456789"
  }
}
```

---

### 10. B 站视频分享重复统计 (`group_data/{group_id}.json`)
* **作用**：记录群聊内分享过的 BV 号及首次分享者与次数。
```json
[
  {
    "bvid": "BV1xx411c7xx",
    "first_sharer": "123456789",
    "timestamp": 1795100000,
    "count": 3
  }
]
```

---

### 11. 情绪好感度数据库 (`affinity_root/affinity_db.json`)
* **作用**：持久化每位用户对机器人的好感度数值与最近更新时间。
```json
{
  "123456789": {
    "affinity": 85,
    "nickname": "鱼饼",
    "updated_at": "2026-11-20T14:00:00"
  }
}
```

---

### 12. 话题记忆数据库 (`topic_memory_root/topic_db.json`)
* **作用**：保存由对话中提取出的长期话题记忆及词向量（Embedding）。
```json
{
  "aiocqhttp:group_message:123456": [
    {
      "id": "topic_1795100000_123",
      "keyword": "开源发布会",
      "text": "群友讨论了关于开源发布会的时间定在下周五，决定在路演厅举行。",
      "owner_id": "123456789",
      "vector": [0.0123, -0.0456, 0.0789, "..."],
      "created_at": "2026-11-20T10:00:00"
    }
  ]
}
```

---

### 13. 私聊个人记忆库 (`personal_memory_root/personal_memories/{user_id}.json`)
* **作用**：仅私聊，记录私聊用户的原子事实、偏好、习惯与雷点。
```json
[
  {
    "id": "pm_1795100000_123",
    "category": "preference",
    "text": "平时喝咖啡只加燕麦奶，不喜欢太甜的饮料",
    "importance": 4,
    "vector": [0.0321, -0.0154, 0.0892, "..."],
    "created_at": "2026-11-20T11:00:00",
    "updated_at": "2026-11-20T11:00:00"
  }
]
```

---

### 14. 演化自我设定 (`evolved_persona/evolved_persona.json`)
* **作用**：存储模型根据自身聊天表现自主演化出的一小段“自我设定”。
```json
{
  "persona": "说话语气偏向轻松随和，偶尔会带一点点调皮的尾音~ 特别喜欢在傍晚喝柠檬红茶看落日。",
  "reason": "在近期的对话中自然流露出对傍晚美景与茶歇的喜爱，语气自然形成了轻快温柔的风格",
  "updated_at": "2026-11-20T12:00:00"
}
```

---

### 15. 表情包记录数据库 (`meme_data/meme_records.json`)
* **作用**：记录偷取的表情包文件相对路径、多模态视觉描述、情绪倾向与标签。
```json
[
  {
    "id": "meme_1795100000_abc",
    "path": "data/plugin_data/astrbot_plugin_Fiscok/meme_data/images/meme_1795100000_abc.jpg",
    "description": "一只圆滚滚的猫咪趴在键盘上打盹，表情非常满足惬意",
    "emotion": "惬意/开心",
    "tags": ["猫咪", "打瞌睡", "萌", "可爱"],
    "created_at": "2026-11-20T13:00:00"
  }
]
```

---

### 16. WebUI 提示词库持久化 (`prompt_library.json`)
* **作用**：在 WebUI「提示词」页统一编辑并落盘的模板库。
* **包含键名**：
  * `persona_evolution_prompt` / `persona_evolution_system`：人设演化提示词
  * `emotion_analysis_prompt` / `emotion_analysis_system`：情绪好感度分析
  * `emotion_state_injection`：会话情绪状态与四阶羁绊指南注入模板
  * `topic_memory_analysis_prompt` / `topic_memory_injection_head`：话题记忆抽取与注入
  * `topic_tracking_prompt` / `topic_tracking_reply_injection`：群聊续聊判定与回复
  * `topic_progression_protocol`：话题自动行进连续任务协议
  * `clm_compaction_prompt`：CLM 自主工作记忆账本整理与剪枝模板
  * `bot_daily_schedule_prompt`：机器人自主虚拟生活日程规划模板
  * `proactive_chat_prompt` / `private_proactive_chat_prompt`：群聊与私聊主动发起聊天
  * `care_reminder_template`：待办约定与情感关怀备忘注入模板
  * `personal_memory_extraction_prompt` / `personal_memory_system`：个人长期记忆抽取与系统设定
  * `meme_analysis_prompt` / `meme_analysis_system`：表情包多模态分析
