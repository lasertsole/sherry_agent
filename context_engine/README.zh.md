# MesMemory — 会话消息记忆系统

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **MesMemory** 是 EMA AI Agent 的短期对话记忆引擎（即 `context_engine` 包）：基于 SQLite 持久化会话消息、历史检索与 FTS5 全文搜索。该包还包含负责后台技能维护的 **Curator** 子包 —— 参见 [Curator（技能维护子包）](#curator技能维护子包)。

---

## 目录

- [概述](#概述)
- [包结构](#包结构)
- [数据模型](#数据模型)
- [核心功能](#核心功能)
- [集成点](#集成点)
- [Curator（技能维护子包）](#curator技能维护子包)
- [API 参考](#api-参考)
- [FAQ](#faq)
- [技术栈](#技术栈)

---

## 概述

### 设计定位

MesMemory 是一个**面向单会话的短期消息存储**，设计刻意保持简单：所有存储与检索均基于 SQL/FTS5 —— 本包中没有任何向量嵌入、图算法或重排序器（reranker）。

| | MesMemory |
|---|-----------|
| 范围 | 每个会话的原始 `human` / `ai` / `tool` 消息 |
| 存储 | 共享的单一 SQLite 数据库（`src/store/mes_memory/mes_memory.db`） |
| 检索 | 最近 N 轮、轮次范围查询、分页历史、FTS5 全文搜索 |
| 写入 | `await add_messages(...)` —— 一次调用持久化一轮 |

对 Agent 自建技能的长期维护（生命周期流转、合并、清理）由 `context_engine/` 内独立的 [Curator](#curator技能维护子包) 子包负责 —— 它**不会**触碰消息数据。

### 核心能力

1. **消息持久化** — 将每轮对话的 `human`/`ai`/`tool` 消息写入 SQLite
2. **历史检索** — 按最近 N 轮、轮次范围或分页方式获取历史消息
3. **全文搜索** — 基于 FTS5 的对话搜索，为 CJK 查询提供 trigram 路径、LIKE 降级与上下文预览
4. **会话管理** — 列出顶层会话（含派生标题）；删除某个会话的全部消息

---

## 包结构

```
context_engine/
├── __init__.py          # 包导出（re-export store 与 core 的 API）
├── core.py              # 业务层：历史格式化、FTS5 搜索
├── store/
│   ├── __init__.py      # 存储层导出
│   ├── db.py            # SQLite 连接、WAL 模式、版本化迁移（建表、索引、FTS5 触发器）
│   └── core.py          # 消息 CRUD：新增/查询/删除 + 会话列举
└── curator/             # 后台技能维护编排器（有独立 README）
```

```
┌──────────────────────────────────────────────────────┐
│                    context_engine                    │
├──────────────────────┬───────────────────────────────┤
│   store/  （数据层）  │     core.py  （业务层）        │
├──────────────────────┼───────────────────────────────┤
│ • db.py              │ • retrieve_history_by_last_   │
│   - SQLite 连接       │   n_prompt() → 格式化对话      │
│   - WAL + 迁移        │ • search_messages() → FTS5 /  │
│ • core.py            │   trigram / LIKE 路由          │
│   - add_messages     │ • _sanitize_fts5_query()      │
│   - 轮次查询          │   查询净化                     │
│   - 分页历史          │ • _decode_content()           │
│   - 会话列举          │   JSON 内容解码                │
└──────────────────────┴───────────────────────────────┘
```

### 包导出（`__init__.py`）

```python
# context_engine/__init__.py
from .store import *   # get_db, add_messages, get_messages_by_lastest_n_turns,
                       # get_turns_by_turn_num_scope, get_history_by_turn_page,
                       # get_session_ids, delete_messages_by_session
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## 数据模型

### 数据库 Schema

```sql
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,   -- 轮次序号（一次 add_messages 调用 = 一轮）
    session_id    TEXT NOT NULL,      -- 会话 ID
    role          TEXT NOT NULL,      -- human / ai / tool
    content       TEXT,               -- 消息内容（json.dumps, ensure_ascii=False）
    tool_call_id  TEXT,               -- 工具调用 ID（tool 消息）
    tool_calls    TEXT,               -- 工具调用详情，JSON（AI 消息）
    tool_status   TEXT,               -- 工具执行状态（默认 "success"）
    tool_name     TEXT,               -- 工具名称
    timestamp     TEXT NOT NULL,      -- 时间戳 YYYYMMDDHHmmss（同批消息共享）
    finish_reason TEXT,               -- AI 响应终止原因
    reasoning     TEXT,               -- 思维链（additional_kwargs["reasoning_content"]）
    reasoning_content TEXT,           -- 推理过程
    images        TEXT,               -- 图片路径 JSON 列表（human 多模态输入）
    audios        TEXT,               -- 音频路径/引用 JSON 列表
    videos        TEXT,               -- 视频路径/引用 JSON 列表
    model_name    TEXT,               -- AI 消息：产生响应的模型
    input_tokens  INTEGER,            -- AI 消息：usage_metadata 输入 token
    output_tokens INTEGER,            -- AI 消息：usage_metadata 输出 token
    origin        TEXT                -- 消息来源标记（完成载体为 "subagent_completion"，其余为 NULL）
);
```

**索引：**

- `idx_messages_timestamp` — `(session_id, timestamp)`
- `idx_messages_turn_num` — `(session_id, turn_num)`

**FTS5 表**（两者均索引 `content`、`tool_name`、`tool_calls` 的拼接文本）：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**FTS5 触发器：** 每个 FTS 表都在 `messages` 上建有 `AFTER INSERT` / `AFTER UPDATE` / `AFTER DELETE` 触发器，自动同步索引。因此删除行（例如 `delete_messages_by_session`）无需单独清理 FTS。

**迁移：** 建表过程通过 `_migrations` 表做版本化管理。步骤依次为：
`build_messages_tb` → `build_messages_fts_tb` → `build_messages_fts_trigram_tb` → `add_images_column` → `add_audio_video_columns` → `add_model_token_columns` → `add_origin_column`。

---

## 核心功能

### 1. 消息持久化

```python
from context_engine.store import add_messages

# 持久化一轮对话（turn_num 每次调用自动递增）
await add_messages("session_001", [user_msg, ai_msg])
```

- 一次 `add_messages` 调用 = 一轮：同批消息共享相同 `turn_num` 与相同 `YYYYMMDDHHmmss` 时间戳
- 由摘要压缩产生的 `human` 消息（以 `additional_kwargs["lc_source"] == "summarization"` 识别）会被过滤掉
- `ai` 消息持久化 `tool_calls`（JSON）、来自 `additional_kwargs["reasoning_content"]` 的思维链（存入 `reasoning` 列），以及响应与用量元数据中的 `model_name` / `input_tokens` / `output_tokens`（均可选，缺失时为 `None`）
- `human` 消息将 `additional_kwargs` 中的多模态文件引用持久化到 `images` / `audios` / `videos` 列（JSON 列表，为空时是 `None`）
- `tool` 消息持久化 `tool_call_id`、`tool_name` 与 `tool_status`（默认 `"success"`）
- 元数据满足 `internal: true` 且 `provenance: "subagent_completion"` 的 `human` 消息（steering 队列的完成载体）以 `origin = 'subagent_completion'` 持久化；其余行的 `origin` 均为 `NULL`（不会是空字符串，也不会是 JSON）

### 2. 历史检索

```python
from context_engine import retrieve_history_by_last_n_prompt

# 获取最近 5 轮对话，格式化为 prompt 字符串
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**输出格式**（与 `core.py` 逐字一致；每轮正文不含时间戳）：

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
user: User message

agent: AI response
</turn>

...

===== The above is the content of the last 5 turns =====

```

若 `human` 消息内容是多模态列表，则只取第一个 `{"type": "text"}` 部分。

也支持按轮次范围查询：

```python
from context_engine.store import get_turns_by_turn_num_scope

# 获取 target_turn_num 前后各 5 轮的记录
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

分页历史查询（第 1 页为最近一页）：

```python
from context_engine.store import get_history_by_turn_page

# 获取第 1 页，每页 10 轮
rows = get_history_by_turn_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

轮次范围查询与分页查询均按轮次从新到旧返回，且 JSON 编码的 `content`、`tool_calls`、`images`、`audios`、`videos` 列会被解码为 Python 对象。

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
    print(r["snippet"])        # 高亮片段（标记：>>> match <<<）
    print(r["context"])        # 最多 3 条：上一条消息、匹配消息、下一条消息
```

**搜索特性：**

- **双 FTS5 表**：`messages_fts`（默认 unicode61 分词器）与 `messages_fts_trigram`（trigram 分词器，支持 CJK 子串匹配）
- **自动路由**：非 CJK 查询走 `messages_fts`；CJK 字符总数 ≥3 且没有短于 3 个 CJK 字符的 token 的 CJK 查询走 trigram 表；其余情况降级为 LIKE
- **逐 token CJK 检查**：如 `广西 OR 桂林 OR 漓江` 等多词查询逐 token 检查 —— 任一 CJK token 少于 3 个 CJK 字符，整条查询就走 LIKE（trigram 要求每个 token ≥3 个 CJK 字符）
- **LIKE 降级**：对每个非运算符 token，在 `content`、`tool_name`、`tool_calls` 上各建一个 LIKE 条件（带 `ESCAPE '\'`），按 `timestamp DESC` 排序；snippet 是以首个 token 出现位置为中心的 120 字符窗口
- **查询净化**（`_sanitize_fts5_query`）：保留成对引号短语、剥离未配对的 FTS5 特殊字符、合并连续 `*`、移除悬挂的 `AND`/`OR`/`NOT`，并为含连字符/点号/下划线的词加引号（如 `my-app.config.ts`），使 FTS5 按短语匹配
- **Trigram token 加引号**：trigram 路径上每个非运算符 token 都用双引号包裹，同时保留布尔运算符（`AND`、`OR`、`NOT`）
- **上下文扩展**：每个匹配结果附带最多 3 条上下文 —— 前一条消息、匹配消息本身、后一条消息（按 `timestamp` 再按 `id` 排序），每条渲染为 `{"role": ..., "content": preview}`，preview 截断到 200 字符；多模态列表内容渲染其文本部分，若无文本则显示 `[multimodal content]`
- **结果精简**：完整 `content` 字段会从结果中移除（仅保留 snippet 与 context），节省 token
- **容错**：空查询/净化后为空的查询返回 `[]`；MATCH 触发的 FTS5 `sqlite3.OperationalError` 被吞掉并返回 `[]`
- **线程安全**：所有数据库访问由模块级 `threading.Lock` 保护
- **排序**：FTS5 路径按相关度排序（`ORDER BY rank`）；LIKE 路径按 `timestamp DESC` 排序

---

## 集成点

经验证的 `context_engine` 包使用方：

| 入口 | 导入 | 用途 |
|------|------|------|
| `agent/middlewares/context_engine/core.py` → `ContextEngineHook` | `add_messages` | Agent 中间件（在 `agent/core.py` 的主 Agent 中注册）。在 `aafter_agent` 中切出最后一轮（`slice_last_turn`）、净化（`sanitize_tool_use_result_pairing`）后通过 `add_messages()` 持久化；同时注入系统提示词（`wrap_model_call`/`awrap_model_call`），并维护记忆/技能 nudge 计数器（阈值 10）与 nudge 子 Agent。详见 `agent/middlewares/README.md`。 |
| `agent/tools/message_search.py` → `message_search` 工具 | `get_db`、`search_messages`、`get_turns_by_turn_num_scope` | 跨会话回忆工具：FTS5 搜索（limit 50）→ 按匹配取轮次范围 → LLM 会话摘要；无 query 时改为返回最近会话的元数据 |
| `server/service/messages.py` | `get_session_ids`、`get_history_by_turn_page`，以及（来自 `context_engine.curator` 的）`reset_idle_for_seconds` | 面向客户端的会话列表（顶层会话 + 派生标题）、分页历史，以及每次用户回合重置 curator 空闲计时 |
| `server/DAO/messages.py` | `delete_messages_by_session` | 「清空会话」操作 |
| `server/trigger/http/stats.py` | `get_db`（来自 `context_engine.store.db`） | 基于 messages 表的使用统计 |
| `server/__main__.py` | `import context_engine.curator` | 导入 curator 包即启动其后台守护线程 |

---

## Curator（技能维护子包）

`context_engine/curator/` 是一个**后台技能维护编排器** —— 与消息存储无关。经验证的行为摘要：

- **范围**：只作用于 `skills/auto/` 下的 Agent 自建技能；绝不触碰内置技能
- **触发**：导入 `context_engine.curator` 即启动守护线程（`curator-timer`），每 3600 秒调用一次 `maybe_run_curator()`；仅当 `should_run_now()` 为真（已启用、未暂停、`interval_hours` 已到期）且 Agent 空闲足够久（`min_idle_hours`）时才执行。每次用户回合都会调用 `reset_idle_for_seconds()`（`server/service/messages.py`）
- **生命周期**：`active → stale`（`stale_after_days`，默认 30 天无活动）；超过 `archive_after_days`（默认 90 天）的技能会从磁盘移除；处于 stale 窗口内但从未使用过的技能会被重新激活。被 pinned 的技能跳过所有流转
- **LLM 合并**（通过 `curator.yaml` 可选开启，默认 `consolidate: false`）：将重叠的窄技能合并为 LLM 生成的 umbrella 技能
- **状态与报告**：运行状态存于 `skills/.curator_state`；报告位于 `logs/curator/{timestamp}/`（`run.json` + `REPORT.md`）

公开 API 包括 `run_curator_review(on_summary=None, dry_run=False, consolidate=None)`、`maybe_run_curator(*, idle_for_seconds=None, on_summary=None)`、`reset_idle_for_seconds()`、`pin_skill(name)`、`unpin_skill(name)`、`delete_skill(name, absorbed_into="")`、`apply_automatic_transitions(now=None)` 与 `should_run_now(now=None)`。

▶️ 完整文档：[curator/README.md](curator/README.md) · [中文](curator/README.zh.md) · [한국어](curator/README.ko.md) · [日本語](curator/README.ja.md)

---

## API 参考

以下签名均复制自源码，并标注各层导入路径。

### 业务层（`context_engine.core`，包级别 re-export）

#### `retrieve_history_by_last_n_prompt(session_id: str, n: int = 5) -> str`
将最近 `n` 轮对话格式化为 prompt 字符串（输出格式见上文）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `n` | `int` | 轮次数（默认 5） |

**返回：** `str` — 格式化后的对话历史

---

#### `search_messages(query: str, session_id: str, role_filter: list[str] = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]`
全文搜索消息。

| 参数 | 类型 | 说明 |
|------|------|------|
| `query` | `str` | 搜索查询（为空 → `[]`） |
| `session_id` | `str` | 会话 ID |
| `role_filter` | `list[str]` | 角色过滤（如 `["human", "ai"]`；默认 `None`） |
| `limit` | `int` | 返回条数上限（默认 20） |
| `offset` | `int` | 偏移量（默认 0） |

**返回：** `list[dict[str, Any]]` — 每个结果包含 `id`、`session_id`、`turn_num`、`role`、`snippet`、`timestamp`、`tool_name`、`context`（完整 `content` 字段已被移除）

---

#### `_sanitize_fts5_query(query: str) -> str`（内部）
净化用户输入以安全用于 FTS5 MATCH 查询。

#### `_decode_content(content: Any) -> Any`（内部）
解码携带 `\x00json:` 前缀的消息内容字符串；其他值原样返回。

---

### 存储层（`context_engine.store`）

#### `async add_messages(session_id: str, messages: list[BaseMessage]) -> None`
将一批 LangChain 消息作为新的一轮持久化。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `messages` | `list[BaseMessage]` | LangChain `BaseMessage` 列表（`human` / `ai` / `tool`） |

---

#### `get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5) -> list[dict]`
获取最近 `last_n` 轮的消息行（内部委托 `get_history_by_turn_page` 第 1 页）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `last_n` | `int` | 轮次数（默认 5） |

**返回：** `list[dict]` — 消息行，按轮次从新到旧，JSON 列已解码

---

#### `get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]`
获取目标轮次前后一定范围内的消息（范围会被收敛到 `[1, max_turn_num]`）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `target_turn_num` | `int` | 目标轮次号 |
| `half_scope` | `int` | 前后各多少轮（默认 5） |

**返回：** `list[dict]` — 消息行，按轮次从新到旧，JSON 列已解码

---

#### `get_history_by_turn_page(session_id: str, min_turn_num: Annotated[int, Field(ge=1)] = 1, turn_page_size: Annotated[int, Field(ge=1)] = 10, turn_page_num: Annotated[int, Field(ge=1)] = 1) -> list[dict]`
按轮次从最新一页向前分页获取历史（带 `@validate_call` 装饰）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 ID |
| `min_turn_num` | `int` | `turn_num` 的包含式下界（≥1，默认 1） |
| `turn_page_size` | `int` | 每页轮次数（≥1，默认 10） |
| `turn_page_num` | `int` | 从最新一轮向前数的 1 起始页码（≥1，默认 1） |

**返回：** `list[dict]` — 消息行，按轮次从新到旧，JSON 列已解码

---

#### `get_max_turn_num(session_id: str) -> int`
会话的最大 `turn_num`；无消息时返回 `0`。定义于 `context_engine/store/core.py`（未被 `context_engine.store` re-export）。

---

#### `delete_messages_by_session(session_id: str) -> int`
删除某会话的全部消息。FTS5 索引由触发器自动清理。

**返回：** `int` — 删除的行数

---

#### `get_session_ids() -> list[dict]`
列举所有不同的顶层会话（排除包含 `:subagent:` 的子 Agent 会话），按最近活动排序。

**返回：** `list[dict]` — 每项为 `{"session_id": str, "last_time": str, "title": str}`，其中 `last_time` 是最新的 `YYYYMMDDHHmmss` 时间戳，`title` 从最近一条 `human` 消息派生（可能为 `""`）

标题查询只统计 `origin IS NULL` 的行；如果某个会话的 `human` 行全部是 `subagent_completion` 载体，其标题为空字符串，由客户端渲染占位符。

---

#### `get_db()`（`context_engine.store.db`）
返回共享的 `sqlite3.Connection`（首次调用时创建，参数：`check_same_thread=False`、`timeout=1.0`、`isolation_level=None`、`row_factory=sqlite3.Row`、`PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`）。

---

## FAQ

### Q1: MesMemory 和 Curator 是什么关系？

它们在同一个包里，但运行时互不相关：MesMemory 负责原始会话消息的存储与检索（短期记忆）；Curator 负责 `skills/auto/` 下 Agent 自建技能的维护（生命周期流转、合并、清理）。Curator 从不读写 `messages` 表。

---

### Q2: 为什么需要两套 FTS5 表？

`messages_fts` 使用默认的 unicode61 分词器，适合英文式 token 匹配。`messages_fts_trigram` 使用 trigram 分词器，将文本切成 3-gram 子串，从而支持 CJK 子串匹配（unicode61 会把 CJK 文本拆成单字，产生误报）。路由器根据查询的 CJK 内容与 token 长度自动选择表。

---

### Q3: 搜索结果的 `snippet` 和 `content` 有什么区别？

FTS5 路径上，`snippet` 是 FTS5 提供的带 `>>>` / `<<<` 高亮标记的摘录（40 token 窗口）。LIKE 路径上，`snippet` 是以首个 token 出现位置为中心的 `content` 120 字符切片（无标记）。为节省 token，完整 `content` 字段会从所有结果中移除；需要完整内容时请使用 `get_messages_by_lastest_n_turns` / `get_history_by_turn_page`。

---

### Q4: 逐 token CJK 路由是如何工作的？

对于 CJK 查询，系统逐个检查每个非运算符 token。如果任一 CJK token 少于 3 个 CJK 字符，trigram FTS5 无法匹配（要求每个 token ≥3 个 CJK 字符），因此整条查询降级为 LIKE 搜索。这解决了如 `"广西 OR 桂林 OR 漓江"` 等每个词仅 2 个 CJK 字符的情况（即使 CJK 字符总数为 6）。

---

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| **数据库** | SQLite 3 — WAL 模式、`foreign_keys=ON`、单一共享连接（`check_same_thread=False`、`timeout=1.0`） |
| **全文搜索** | FTS5（unicode61）+ FTS5（trigram 分词器） |
| **消息模型** | LangChain `BaseMessage` |
| **参数校验** | Pydantic `@validate_call`（用于 `get_history_by_turn_page`） |
| **并发控制** | 所有数据库访问由 `threading.Lock` 保护 |
| **存储路径** | `src/store/mes_memory/mes_memory.db`（`config.path.SRC_DIR / "store/mes_memory/mes_memory.db"`） |

---

## 许可证

本项目遵循 EMA AI Agent 的开源协议。

---

**最后更新：** 2026-09-02
