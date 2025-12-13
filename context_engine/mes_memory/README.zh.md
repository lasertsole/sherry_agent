# MesMemory — 会话消息记忆系统

> **MesMemory** 是 EMA AI Agent 的短期对话记忆引擎，负责消息的持久化存储、历史检索、全文搜索以及用户偏好提取。

---

## 目录

- [概述](#概述)
- [核心架构](#核心架构)
- [数据模型](#数据模型)
- [核心功能](#核心功能)
- [API 参考](#api-参考)
- [FAQ](#faq)

---

## 概述

### 设计定位

MesMemory 与 [Skill Memory](../skill_memory/README.ch.md) 互为补充：

| Skill Memory | MesMemory |
|-------------|-----------|
| 长期知识图谱（TASK/SKILL/EVENT） | 短期会话消息存储 |
| 结构化三元组，跨 session 复用 | 原始消息序列，按 session 隔离 |
| 图社区 + PageRank 召回 | FTS5 全文搜索 + 轮次范围查询 |
| 异步后台提取 | 同步写入，立即持久化 |

### 核心能力

1. **消息持久化** — 将每轮对话的 human/ai/tool 消息写入 SQLite
2. **历史检索** — 按最近 N 轮次或指定轮次范围获取历史消息
3. **全文搜索** — 基于 FTS5 的对话搜索，支持中文分词（trigram）和上下文预览
4. **记忆 nudging** — 定期触发用户偏好提取，将对话中的偏好写入长期 memory store

---

## 核心架构

```
┌────────────────────────────────────────────────────┐
│                   MesMemory Core                    │
├───────────────────┬────────────────────────────────┤
│    store/         │          core.py                │
│   (数据层)        │        (业务逻辑层)              │
├───────────────────┼────────────────────────────────┤
│ • db.py           │ • retrieve_history_by_last_n   │
│   - SQLite 连接    │   _prompt() → 历史格式化       │
│   - 迁移管理       │ • search_messages() → FTS5    │
│ • core.py         │   搜索 + 上下文预览              │
│   - CRUD 操作      │ • nudge_messages() →          │
│   - 消息写入        │   触发偏好提取                  │
│   - 轮次查询        │                              │
└───────────────────┴────────────────────────────────┘
```

### 存储层（store/）

| 文件 | 职责 |
|------|------|
| `store/db.py` | SQLite 连接管理、WAL 模式、自动迁移（建表、索引、FTS5 触发器） |
| `store/core.py` | 消息 CRUD：`add_messages`、`get_messages_by_lastest_n_turns`、`get_turns_by_turn_num_scope`、`update_session` |

### 业务层（core.py）

| 函数 | 职责 |
|------|------|
| `retrieve_history_by_last_n_prompt(session_id, n)` | 获取最近 N 轮对话并格式化为 prompt 上下文 |
| `search_messages(query, session_id, ...)` | FTS5 全文搜索，支持中文 trigram、上下文扩展 |
| `append_messages(session_id, messages)` | 写入消息并触发 nudge 检查 |
| `nudge_messages(session_id, ...)` | 检查是否达到 nudge 轮次阈值，触发偏好提取 |

---

## 数据模型

### 数据库 Schema

```sql
-- 会话表
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    nudge_turn_num INTEGER NOT NULL DEFAULT 0
);

-- 消息表
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,       -- 轮次序号
    session_id    TEXT NOT NULL,           -- 会话 ID
    role          TEXT NOT NULL,           -- human / ai / tool
    content       TEXT,                   -- 消息内容（JSON 编码）
    tool_call_id  TEXT,                   -- 工具调用 ID
    tool_calls    TEXT,                   -- 工具调用详情（JSON）
    tool_status   TEXT,                   -- 工具执行状态
    tool_name     TEXT,                   -- 工具名称
    timestamp     TEXT NOT NULL,          -- 时间戳（YYYYMMDDHHmmss）
    finish_reason TEXT,                   -- AI 响应终止原因
    reasoning     TEXT,                   -- 推理内容
    reasoning_content TEXT                -- 推理过程
);

-- FTS5 全文搜索（英文优先）
CREATE VIRTUAL TABLE messages_fts USING fts5(content);

-- FTS5 中文 trigram 搜索
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**索引：**
- `idx_messages_timestamp` — `(session_id, timestamp)` 用于快速按时间范围查询
- `idx_messages_turn_num` — `(session_id, turn_num)` 用于按轮次查询

**FTS5 触发器：** messages 表发生 INSERT/UPDATE/DELETE 时自动同步维护 FTS5 索引，索引字段包括 `content`、`tool_name`、`tool_calls`。

---

## 核心功能

### 1. 消息持久化

```python
from context_engine.mes_memory import append_messages

# 写入一轮对话消息（自动递增 turn_num）
await append_messages("session_001", [user_msg, ai_msg])
```

- human/ai/tool 三种角色消息均被持久化
- 自动从 `lc_source == "summarization"` 的 human 消息（压缩摘要来源）
- 每条消息携带 `YYYYMMDDHHmmss` 时间戳

---

### 2. 历史检索

```python
from context_engine.mes_memory import retrieve_history_by_last_n_prompt

# 获取最近 5 轮对话，格式化为 prompt
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**输出格式：**

```
===== 以下是 前5轮对话内容 (从旧到新，时间戳timestamp格式为 YYYYMMDDHHmmss) =====

<turn>
User: 用户消息

Assistant: AI 回复
</turn>

...

===== 以上是 前5轮对话内容 =====
```

也支持按轮次范围查询：

```python
from context_engine.mes_memory.store import get_turns_by_turn_num_scope

# 获取 target_turn_num 前后各 5 轮的记录
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

---

### 3. 全文搜索

```python
from context_engine.mes_memory import search_messages

# 搜索包含 "Docker" 的消息，带上下文预览
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # 高亮片段
    print(r["context"])        # 前后各 1 条消息的上下文
```

**搜索特性：**

- **双 FTS5 表**：`messages_fts`（默认 unicode61 分词）和 `messages_fts_trigram`（trigram 分词，支持中文）
- **自动路由**：检测到中文查询（3 个以上 CJK 字符）自动走 trigram 路径，否则走默认 FTS5
- **智能降级**：短中文查询（<3 CJK 字符）自动降级为 LIKE 查询
- **查询净化**：自动处理 FTS5 特殊字符、引号平衡、布尔运算符清理
- **上下文扩展**：每条结果自动附带前后各 1 条消息作为上下文
- **多模态友好**：对包含图片等非文本内容的消息，显示 `[multimodal content]` 标记
- **结果精简**：返回的 matches 不包含完整 `content` 字段（仅提供 snippet 和 context），节省 token

---

### 4. 记忆 Nudging（偏好提取）

```python
from context_engine.mes_memory import nudge_messages, append_messages

# 写入消息时自动检查 nudge
await append_messages("session_001", messages, nudge_turn=10)

# 或手动触发
await nudge_messages("session_001", nudge_turn=10, skip_last_turn=False)
```

**工作流程：**

```
写入消息 → 检查当前轮次与上次 nudge 轮次的差值
    ↓ 差值 < nudge_turn（默认 10）
    跳过
    ↓ 差值 ≥ nudge_turn
    1. 获取从上次 nudge 至今的所有消息
    2. 过滤掉 tool 消息，只保留 human/ai
    3. 按 turn 和 role 合并为 BaseMessage
    4. 加载现有 memory store 中的偏好
    5. 调用 extract_memory_agent（LLM）
    6. agent 调用 memory 工具写入/更新用户偏好
    7. 更新 sessions 表的 nudge_turn_num
```

**特点：**
- 增量处理：只处理上次 nudge 之后的新对话
- 去重：agent 被指示不添加已存在的偏好
- 容量管理：偏好满时自动合并或移除旧的
- 聪明过滤：跳过已被用户通过 memory 工具手动更新的项

---

## API 参考

### `append_messages(session_id, messages, nudge_turn=10)`
写入消息并触发 nudge 检查。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage 列表 |
| `nudge_turn` | `int` | nudge 检查间隔（默认 10 轮） |

---

### `retrieve_history_by_last_n_prompt(session_id, n=5)`
获取最近 N 轮对话并格式化为 prompt 字符串。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `n` | `int` | 轮次数（默认 5） |

**返回：** `str` — 格式化后的对话历史

---

### `search_messages(query, session_id, role_filter=None, limit=20, offset=0)`
全文搜索消息。

| 参数 | 类型 | 说明 |
|------|------|------|
| `query` | `str` | 搜索查询 |
| `session_id` | `str` | 会话 ID |
| `role_filter` | `list[str]` | 角色过滤（如 `["human", "ai"]`） |
| `limit` | `int` | 返回条数上限（默认 20） |
| `offset` | `int` | 偏移量（默认 0） |

**返回：** `list[dict]` — 每个结果包含 `id`, `session_id`, `turn_num`, `role`, `snippet`, `timestamp`, `tool_name`, `context`

---

### `nudge_messages(session_id, skip_last_turn=False, nudge_turn=10)`
手动触发用户偏好提取。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `skip_last_turn` | `bool` | 是否跳过最新一轮 |
| `nudge_turn` | `int` | nudge 检查间隔 |

---

### `get_messages_by_lastest_n_turns(session_id, last_n=5)`
从 store 层直接获取最近 N 轮原始消息记录。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `last_n` | `int` | 轮次数（默认 5） |

**返回：** `list[dict]` — 每条记录包含完整的消息字段

---

### `add_messages(session_id, messages)`
（store 层）写入消息到数据库，不触发 nudge。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage 列表 |

---

### `update_session(session_id, params)`
更新会话属性（如 nudge_turn_num）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `params` | `dict` | 要更新的字段键值对（`session_id` 字段被禁止更新） |

---

## FAQ

### Q1: MesMemory 和 Skill Memory 的关系是什么？

MesMemory 负责**原始消息存储与检索**（短期记忆），Skill Memory 负责**知识提取与图谱构建**（长期记忆）。MesMemory 存储的是"原话"，Skill Memory 存储的是从原话中提炼的结构化知识。

---

### Q2: 为什么需要两套 FTS5 表？

`messages_fts` 使用默认的 unicode61 分词器，适合英文和拼音搜索。`messages_fts_trigram` 使用 trigram 分词器，将文本切成 3-gram 子串，天然支持中文模糊匹配和子串搜索。系统根据查询语言自动选择。

---

### Q3: 搜索结果的 `snippet` 和 `content` 有什么区别？

`snippet` 是 FTS5 提供的带高亮标记的简短片段（前后各 40 字符），用于快速预览匹配位置。`content` 是完整的消息内容，但在搜索结果中被移除（以节省 token），开发者如需完整 content 应通过 `get_messages_by_lastest_n_turns` 获取。

---

### Q4: nudge_turn 设置为多大合适？

默认 10 轮。太频繁（如 1-3 轮）会导致每轮都调用 LLM 提取偏好，增加成本。太长（如 30+ 轮）可能遗漏用户的短期行为变化。建议 5-15 轮之间。

---

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| **数据库** | SQLite 3 + WAL 模式 |
| **全文搜索** | FTS5 + Trigram 分词 |
| **框架** | LangChain BaseMessage |
| **存储路径** | `store/mes_memory/mes_memory.db` |

---

## 许可证

本项目遵循 EMA AI Agent 的开源协议。

---

**最后更新：** 2026-05-30
