# MesMemory — 会话消息记忆系统

[**English**](README.md) | **中文文档**

> **MesMemory** 是 EMA AI Agent 的短期对话记忆引擎，负责消息的持久化存储、历史检索、全文搜索。

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

MesMemory 与 [Skill Memory](../skill_memory/README.zh.md) 互为补充：

| Skill Memory | MesMemory |
|-------------|-----------|
| 长期知识图谱（TASK/SKILL/EVENT） | 短期会话消息存储 |
| 结构化三元组，跨 session 复用 | 原始消息序列，按 session 隔离 |
| 图社区 + PageRank 召回 | FTS5 全文搜索 + 轮次范围查询 |
| 异步后台提取 | 同步写入，立即持久化 |

### 核心能力

1. **消息持久化** — 将每轮对话的 human/ai/tool 消息写入 SQLite
2. **历史检索** — 按最近 N 轮次、分页或指定轮次范围获取历史消息
3. **全文搜索** — 基于 FTS5 的对话搜索，支持中文分词（trigram）和上下文预览

---

## 核心架构

```
┌────────────────────────────────────────────────────┐
│                   context_engine                     │
├───────────────────┬────────────────────────────────┤
│    store/         │          core.py                │
│   (数据层)        │        (业务逻辑层)              │
├───────────────────┼────────────────────────────────┤
│ • db.py           │ • retrieve_history_by_last_n   │
│   - SQLite 连接    │   _prompt() → 历史格式化       │
│   - 迁移管理       │ • search_messages() → FTS5    │
│ • core.py         │   搜索 + 上下文预览              │
│   - CRUD 操作      │ • _sanitize_fts5_query()     │
│   - 消息写入        │   查询净化                     │
│   - 轮次查询        │ • _decode_content()          │
│   - 分页历史        │   JSON 内容解码                │
└───────────────────┴────────────────────────────────┘
```

### 存储层（store/）

| 文件 | 职责 |
|------|------|
| `store/db.py` | SQLite 连接管理、WAL 模式、自动迁移（建表、索引、FTS5 触发器） |
| `store/core.py` | 消息 CRUD：`add_messages`、`get_messages_by_lastest_n_turns`、`get_turns_by_turn_num_scope`、`get_history_by_page`、`get_max_turn_num` |

### 业务层（core.py）

| 函数 | 职责 |
|------|------|
| `retrieve_history_by_last_n_prompt(session_id, n)` | 获取最近 N 轮对话并格式化为 prompt 上下文 |
| `search_messages(query, session_id, ...)` | FTS5 全文搜索，支持中文 trigram、上下文扩展 |
| `_sanitize_fts5_query(query)` | 净化用户输入以安全用于 FTS5 MATCH 查询（内部函数） |
| `_decode_content(content)` | 反转 JSON 编码的消息内容（内部函数） |

### 包导出（`__init__.py`）

```python
# context_engine/__init__.py
from .store import *                                              # get_db, add_messages, get_messages_by_lastest_n_turns, get_turns_by_turn_num_scope, get_history_by_page
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## 数据模型

### 数据库 Schema

```sql
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
from context_engine.mes_memory.store import add_messages

# 写入一轮对话消息（自动递增 turn_num）
await add_messages("session_001", [user_msg, ai_msg])
```

- human/ai/tool 三种角色消息均被持久化
- 自动过滤 `lc_source == "summarization"` 的 human 消息（压缩摘要来源）
- 每条消息携带 `YYYYMMDDHHmmss` 时间戳
- 内容使用 `\x00json:` 前缀进行 JSON 编码

---

### 2. 历史检索

```python
from context_engine import retrieve_history_by_last_n_prompt

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

分页历史查询：

```python
from context_engine.mes_memory.store import get_history_by_page

# 获取第 1 页，每页 10 轮
rows = get_history_by_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

---

### 3. 全文搜索

```python
from context_engine import search_messages

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
- **自动路由**：检测到中文查询（每个 token 3 个以上 CJK 字符）自动走 trigram 路径，否则走默认 FTS5
- **逐 token CJK 检查**：如 "广西 OR 桂林 OR 漓江" 等多词查询，逐 token 检查 CJK 长度，任一 token 不足 3 个 CJK 字符则整条查询降级为 LIKE
- **智能降级**：短中文查询（每个 token <3 CJK 字符）自动降级为 LIKE 查询
- **查询净化**：自动处理 FTS5 特殊字符、引号平衡、布尔运算符清理、连字符/点号术语加引号
- **上下文扩展**：每条结果自动附带前后各 1 条消息作为上下文
- **多模态友好**：对包含图片等非文本内容的消息，显示 `[multimodal content]` 标记
- **结果精简**：返回的 matches 不包含完整 `content` 字段（仅提供 snippet 和 context），节省 token
- **线程安全**：所有数据库操作受 threading lock 保护

---

## API 参考

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

### `add_messages(session_id, messages)`
（store 层）写入消息到数据库。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage 列表 |

---

### `get_messages_by_lastest_n_turns(session_id, last_n=5)`
从 store 层直接获取最近 N 轮原始消息记录。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `last_n` | `int` | 轮次数（默认 5） |

**返回：** `list[dict]` — 每条记录包含完整的消息字段

---

### `get_turns_by_turn_num_scope(session_id, target_turn_num, half_scope=5)`
获取目标轮次前后一定范围内的消息。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `target_turn_num` | `int` | 目标轮次号 |
| `half_scope` | `int` | 前后各多少轮（默认 5） |

**返回：** `list[dict]` — 每条记录包含完整的消息字段，JSON 已解码

---

### `get_history_by_page(session_id, min_turn_num=1, turn_page_size=10, turn_page_num=1)`
分页获取历史消息。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `min_turn_num` | `int` | 最小轮次号（≥1，默认 1） |
| `turn_page_size` | `int` | 每页轮次数（≥1，默认 10） |
| `turn_page_num` | `int` | 页码（≥1，默认 1） |

**返回：** `list[dict]` — 每条记录包含完整的消息字段，JSON 已解码

---

### `get_max_turn_num(session_id)`
获取会话的最大轮次号。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |

**返回：** `int` — 最大轮次号，若无消息则返回 0

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

### Q4: 逐 token CJK 路由是如何工作的？

对于中文查询，系统逐个检查每个非运算符 token 的 CJK 字符数。如果任一 CJK token 不足 3 个 CJK 字符，trigram FTS5 无法匹配（要求每个 token ≥3 个 CJK 字符），因此整条查询降级为 LIKE 搜索。这解决了如 `"广西 OR 桂林 OR 漓江"` 等每个词仅 2 个 CJK 字符的情况。

---

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| **数据库** | SQLite 3 + WAL 模式 |
| **全文搜索** | FTS5 + Trigram 分词 |
| **框架** | LangChain BaseMessage |
| **参数校验** | Pydantic `@validate_call` |
| **存储路径** | `store/mes_memory/mes_memory.db` |

---

## 许可证

本项目遵循 EMA AI Agent 的开源协议。

---

**最后更新：** 2026-07-09
