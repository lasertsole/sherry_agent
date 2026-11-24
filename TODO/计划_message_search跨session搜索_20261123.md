# 计划：message_search 跨 session 搜索 + docstring 修复

- 日期：2026-11-23
- 状态：**待评审**
- 来源：message_search 的 FTS/语义搜索都 scope 到当前 session（`WHERE m.session_id = ?`），不能搜索其他 session 的消息内容。docstring 声称"every past session is searchable"但实现不符。
- 风险等级：**低** — 改造现有工具，不改默认行为
- 依赖：无
- 可独立执行：✅

---

## 0. 现状

### 缺口 1：FTS/LIKE/trigram 搜索 scope 到当前 session

三种搜索策略的 WHERE 子句都是 `m.session_id = ?`（`context_engine/core.py:210, 280, 317`），只能搜当前 session 的消息（含压缩的）。无 query 模式的 `_recent_sessions` 只返回其他 session 的标题/预览，不能搜内容。

### 缺口 2：docstring 和实现不符

`_message_search_tool` 的 docstring（`message_search.py:719-746`）声称 "every past session is searchable" 和 "The current session is excluded from results"，但实际 FTS/LIKE/trigram/语义搜索都 scope 到当前 session。

### 不做自动召回注入

自动召回注入（每 turn 语义搜索过往对话并注入 reminder）会干扰正常对话——语义搜索精度不够、用户在场时不需要系统自动回忆、模型已能主动调 message_search。适合 super-agent 长时无人值守场景，不适合正常对话。

---

## 1. 设计

### 1.1 FTS/LIKE/trigram 支持跨 session 搜索

改造 `search_messages`：新增 `cross_session: bool = False` 参数。`cross_session=True` 时 WHERE 子句从 `m.session_id = ?` 改为 `m.session_id != ?`（排除当前 session，搜其他所有 session）。

改造 `message_search` 工具：新增 `cross_session: bool = False` 参数。

不改变默认行为——`cross_session=False` 时仍搜当前 session。

### 1.2 语义搜索支持跨 session

`semantic_search` 已支持 `session_id=None`（搜所有 session），`message_search` 工具的 `cross_session=True` 时传 `session_id=None`。

### 1.3 docstring 修复

更新 `MessageSearchSchema` 和 `_message_search_tool` 的 docstring，准确描述 `cross_session` 参数和默认行为。

---

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `context_engine/core.py` | `_SearchRequest` + 三策略 + `search_messages` 加 `cross_session` 参数 | FTS/LIKE/trigram 跨 session |
| 2 | `context_engine/embeddings/search.py` | 确认 `session_id=None` 已支持（不改代码） | 语义跨 session |
| 3 | `agent/tools/message_search.py` | `MessageSearchSchema` + `SessionSearcher` + `session_search` + `_run_semantic_search` + `_message_search_tool` 加 `cross_session` 参数 | 工具层 |
| 4 | `agent/tools/message_search.py` | docstring 修复：准确描述默认搜当前 session + `cross_session=True` 搜其他 session | docstring 对齐 |

---

## 3. 改动细节

### 3.1 search_messages 跨 session（context_engine/core.py）

#### _SearchRequest 加 cross_session

```python
@dataclass
class _SearchRequest:
    query: str
    session_id: str
    role_filter: list[str] | None = None
    limit: int = 50
    offset: int = 0
    cross_session: bool = False  # 新增
```

#### _FtsStrategy.search（line 314-348）

```python
def search(self, req: _SearchRequest) -> list[dict[str, Any]]:
    with _lock:
        try:
            if req.cross_session:
                session_clause = "m.session_id != ?"
            else:
                session_clause = "m.session_id = ?"

            where_clauses = [session_clause, "messages_fts MATCH ?"]
            params: list = [req.session_id, req.query]
            # ... rest unchanged ...
```

#### _TrigramStrategy.search（line 197-249）

```python
def search(self, req: _SearchRequest) -> list[dict[str, Any]]:
    # ...
    if req.cross_session:
        tri_where = ["m.session_id != ?", "messages_fts_trigram MATCH ?"]
    else:
        tri_where = ["m.session_id = ?", "messages_fts_trigram MATCH ?"]
    # ... rest unchanged ...
```

#### _LikeStrategy.search（line 258-303）

```python
def search(self, req: _SearchRequest) -> list[dict[str, Any]]:
    # ...
    if req.cross_session:
        like_where = ["m.session_id != ?", f"({' OR '.join(token_clauses)})"]
    else:
        like_where = ["m.session_id = ?", f"({' OR '.join(token_clauses)})"]
    # ... rest unchanged ...
```

#### search_messages 函数签名

```python
def search_messages(
    query: str,
    session_id: str,
    role_filter: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    cross_session: bool = False,  # 新增
) -> list[dict[str, Any]]:
    req = _SearchRequest(
        query=query,
        session_id=session_id,
        role_filter=role_filter,
        limit=limit,
        offset=offset,
        cross_session=cross_session,
    )
    strategy = _factory.select(query)
    return strategy.search(req)
```

### 3.2 message_search 工具加 cross_session（message_search.py）

#### MessageSearchSchema 加参数

```python
class MessageSearchSchema(BaseModel):
    query: str | None = Field(
        default=None,
        description="Search query — keywords, phrases, or boolean expressions to find in past sessions. "
        "Omit this parameter entirely to browse recent sessions instead (returns titles, previews, "
        "timestamps with no LLM cost).",
    )
    role_filter: str | None = Field(
        default=None,
        description="Optional: only search messages from specific roles (comma-separated). "
        "E.g. 'user,assistant' to skip tool outputs.",
    )
    limit: int = Field(default=3, description="Max sessions to summarize (default: 3, max: 5).")
    semantic: bool = Field(
        default=False,
        description="Semantic mode: rank past messages by embedding similarity to the "
        "query instead of FTS keyword matching.",
    )
    cross_session: bool = Field(
        default=False,
        description="Search across ALL past sessions instead of just the current one. "
        "Default False searches the current session (including compacted messages). "
        "Set to True when looking for work done in a different session.",
    )
```

#### SessionSearcher.search 传 cross_session

```python
def search(self, query: str, session_id: str, role_filter: str | None,
           cross_session: bool = False) -> list[dict[str, Any]]:
    role_list = None
    if role_filter and role_filter.strip():
        role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

    return search_messages(
        query=query,
        session_id=session_id,
        role_filter=role_list,
        limit=_FTS_MATCH_LIMIT,
        offset=0,
        cross_session=cross_session,  # 新增
    )
```

#### session_search 传 cross_session

```python
def session_search(
    query: str | None, session_id: str, role_filter: str = None,
    limit: int = 3, cross_session: bool = False,  # 新增
) -> str:
    # ...
    raw_results = searcher.search(query, session_id, role_filter, cross_session)
    # ... rest unchanged ...
```

#### _run_semantic_search 传 cross_session

```python
def _run_semantic_search(query: str, session_id: str | None, limit: int,
                          cross_session: bool = False) -> str:
    from context_engine.embeddings import semantic_search
    # cross_session=True 时 session_id=None（搜所有 session）
    search_sid = None if cross_session else session_id
    matches = run_async(semantic_search(query, search_sid, limit))
    # ... rest unchanged ...
```

#### _message_search_tool 传 cross_session

```python
@tool("message_search", args_schema=MessageSearchSchema)
def _message_search_tool(
    query: str | None = None,
    role_filter: str | None = None,
    limit: int = 3,
    semantic: bool = False,
    cross_session: bool = False,  # 新增
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    if semantic:
        if not query:
            return "Error: semantic search requires a query."
        return _run_semantic_search(query, session_id or None, limit, cross_session)
    return session_search(query, session_id, role_filter, limit, cross_session)
```

### 3.3 docstring 修复（message_search.py）

#### _message_search_tool docstring

现有 docstring 声称 "every past session is searchable" 和 "current session is excluded"，改为：

```python
"""Search past conversations, or browse recent sessions. This is your recall tool.

THREE MODES:
1. Recent sessions (no query): Call with no arguments to see what was worked on recently.
   Returns titles, previews, and timestamps. Zero LLM cost, instant.
   Lists OTHER sessions (current session excluded).

2. Same-session search (default, cross_session=False): Search the CURRENT session's
   message history, including messages that were compressed/compacted away (soft-deleted
   but still FTS-indexed). Use this to recall details lost to context compression.

3. Cross-session search (cross_session=True): Search ALL OTHER sessions' messages.
   Use when the user references work done in a different session.
   Works with both keyword (semantic=False) and semantic (semantic=True) modes.

USE THIS PROACTIVELY when:
- The user says 'we did this before', 'remember when', 'last time', 'as I mentioned'
- The user asks about a topic you worked on before but don't have in current context
- The user references a project, person, or concept that seems familiar but isn't in memory
- You want to check if you've solved a similar problem before
- The user asks 'what did we do about X?' or 'how did we fix Y?'
- The user says 'in another session' or 'last time we worked on this'

Don't hesitate to search when it is actually cross-session -- it's fast and cheap.
Better to search and confirm than to guess or ask the user to repeat themselves.

Search syntax: keywords joined with OR for broad recall (elevenlabs OR baseten OR funding),
phrases for exact match ("docker networking"), boolean (python NOT java), prefix (deploy*).
IMPORTANT: Use OR between keywords for best results — FTS5 defaults to AND which misses
sessions that only mention some terms. If a broad OR query returns nothing, try individual
keyword searches in parallel. Returns summaries of the top matching sessions.
When the messages table has no match, the search falls back to the session's newest
checkpoint state (turns not yet persisted); such results carry source=checkpoint.
"""
```

---

## 4. 执行效果

### 同 session 搜索（默认，不变）

```
message_search(query="encoding")
  → cross_session=False → WHERE m.session_id = ?
  → 搜当前 session 的消息（含压缩的）
  → 行为不变
```

### 跨 session 搜索（新增）

```
用户："上次我们在另一个 session 里怎么修的 encoding 问题？"
  → 模型调 message_search(query="encoding", cross_session=True)
  → WHERE m.session_id != ? → 搜其他所有 session 的消息
  → 返回匹配 session 的摘要
```

```
用户："帮我找之前做过的 WebSocket 相关工作"
  → 模型调 message_search(query="WebSocket", cross_session=True, semantic=True)
  → semantic_search(query, session_id=None, limit=3) → 搜所有 session 的 embeddings
  → 返回语义匹配的消息预览
```

### 无 query 模式（不变）

```
message_search()  # 无参数
  → _recent_sessions → 列表其他 session 的标题/预览
  → 行为不变
```

---

## 5. 不改的东西

| 项 | 理由 |
|----|------|
| `message_search` 工具名 | 不改，复用现有工具 |
| 默认行为（cross_session=False） | 现有调用不变 |
| `_recent_sessions` 无 query 模式 | 已经跨 session（列表），不改 |
| `semantic_search` 核心逻辑 | 已支持 session_id=None，不改 |
| MesMemory DB schema | 不改 |
| FTS5 索引 | 不改 |
| `sessions_messages` 父读子 | 已有，不改 |
| ~~自动召回注入（conversation_recall）~~ | 会干扰正常对话，不做。适合 super-agent 场景 |

---

## 6. 测试

| 测试 | 内容 |
|------|------|
| `test_search_messages_cross_session_fts` | cross_session=True → WHERE `m.session_id != ?`，返回其他 session 的消息 |
| `test_search_messages_same_session_default` | cross_session=False（默认）→ WHERE `m.session_id = ?`，只搜当前 session |
| `test_search_messages_cross_session_trigram` | CJK query + cross_session=True → trigram 策略跨 session |
| `test_search_messages_cross_session_like` | CJK query + cross_session=True → like 策略跨 session |
| `test_semantic_search_cross_session` | session_id=None → 搜所有 session 的 embeddings |
| `test_message_search_tool_cross_session` | `cross_session=True` → 搜其他 session |
| `test_message_search_tool_default_same_session` | 不传 cross_session → 搜当前 session（行为不变） |
| `test_message_search_tool_semantic_cross_session` | `semantic=True, cross_session=True` → 语义搜所有 session |
| `test_docstring_mentions_cross_session` | docstring 准确描述三种模式 |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| 跨 session FTS 搜索返回太多结果 | LIMIT 50 + SessionSummarizer 摘要 top N（已有） |
| 跨 session 语义搜索 embedding 表为空 | fail-open（返回空，工具正常返回 "no matches"） |
| 跨 session 搜索泄露其他 session 内容 | MesMemory 是单用户单 DB，无跨租户泄露 |

### Kill criteria

- 若跨 session FTS 搜索性能差（全表扫描），加 `WHERE compacted = 0` 限制只搜未压缩的
- 若跨 session 语义搜索延迟高，加 embedding 缓存

---

## 8. ROI

### 改动量

| 改动 | 行数 |
|------|------|
| search_messages cross_session（3 策略 + 函数签名） | ~30 行 |
| message_search 工具 cross_session | ~20 行 |
| docstring 修复 | ~20 行 |
| 测试 | 9 个 |
| **总计** | **~70 行代码 + 9 个测试** |

### 收益

| 收益 | 影响 |
|------|------|
| **跨 session 搜索** | 模型能搜其他 session 的消息内容——当前完全不能 |
| **docstring 修复** | docstring 和实现对齐，模型知道何时用 cross_session |
| **语义跨 session** | 语义搜索跨 session 找相关过往对话 |

### ROI 判断

**高 ROI**。~70 行改造现有工具，核心改动是三策略 WHERE 子句条件化（~30 行）。零新建工具、零新中间件、零额外 LLM 调用。
