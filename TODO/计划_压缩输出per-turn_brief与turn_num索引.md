# 计划：压缩时输出 per-turn brief + turn_id 索引，让 message_search 语义回溯压缩前会话

- 建档日期：2026-10-21
- 状态：方案设计（v3，改用 `turn_id` + 合并 `get_turn_messages` 到 `message_search`），未开工
- 来源：用户需求 + 调研（message_search 工具 API、turn_id 生命周期、压缩路径 emission、4 个同级项目 turn_id 模型）
- 严重性：中（压缩后原始会话细节丢失，message_search 无法语义回溯被压缩的内容）
- **前置**：`TODO/计划_基础设施_stamp_turn_id到messages表.md`——stamp `turn_id`（per-conversation-turn uuid）到 `messages` 表 + `additional_kwargs`。本计划的 `turn_id` 分组依赖它

---

## 0. 问题

sherry 的滚动摘要（单层、替换式）在压缩时把最旧的 N 条消息折叠成一个 SummaryDoc，原始消息仍在 MesMemory 里（由 `MessagePersistenceMiddleware` 持久化），但**没有 per-turn 的语义索引**——模型想回溯"压缩前某一轮讨论了什么"时，只能用 `message_search` 做 FTS/语义搜索，而：

1. **`message_search` 丢弃了 turn 标识**——FTS 路径把匹配替换成 LLM 摘要字符串，semantic 路径把 `turn_num` 嵌文本里，都不把 turn 标识作为结构化 handle 返回给模型。
2. **没有 per-turn brief 索引**——FTS5 只索引 `messages.content`（整条消息原文），没有 per-turn 的语义摘要索引。
3. **`get_turns_by_turn_num_scope` 未被工具暴露**——按 turn 取回整轮消息的查询已写好，但没有 agent 工具调用它。

### 0.1 用户选择（已确认）

| 决策点 | 选择 | 理由 |
|---|---|---|
| Turn 标识 | **`turn_id`**（per-conversation-turn uuid） | 基础设施计划 stamp 后 `messages` 表有 `turn_id` 列，全局唯一，可直接 `WHERE turn_id = ?` fetch。跟 codex/claudecode/hermes 对齐 |
| Brief 生成 | **LLM 在现有压缩 pass 里多输出** | 压缩 LLM 已在被丢弃切片上跑，让它额外产 per-turn brief。边际多几百 output token |
| 存储 | **新 `turn_summaries` 表** | MesMemory 加一张表 + FTS5 + embeddings |
| 工具合并 | **`get_turn_messages` 合并到 `message_search`** | 加 `turn_id` 参数做 fetch 模式，不新增独立工具。`message_search` 已是多模式工具（browse/FTS/semantic），加 fetch 模式不违和 |

### 0.2 前置依赖

**`TODO/计划_基础设施_stamp_turn_id到messages表.md`** 必须先落地：
- `messages` 表加 `turn_id TEXT` 列（per-conversation-turn uuid，由 `turn_scope` 在 turn 开始时赋值，`_persist_batch` stamp）
- `additional_kwargs["turn_id"]` stamp 到 in-memory BaseMessage（压缩路径从 `messages_to_summarize` 直接读）
- `runtime/hooks.py` 加 `GET_CURRENT_TURN_ID` 缝隙（让 `_persist_batch` 读 `current_turn_id` contextvar）

基础设施落地后，每条 message 行同时有 `turn_num`（per-persist-batch 整数，MesMemory 内部 bookkeeping）和 `turn_id`（per-conversation-turn uuid）。**本计划只用 `turn_id`，不碰 `turn_num`**。

---

## 1. 设计

### 1.1 `_serialize_for_summary` 用 `<turn id="turn_id">` 包裹每个 conversation turn

**文件**：`agent/middlewares/summarization/summary_generation.py:213-274`

LLM 在 `<conversation>` 里看到每个 turn 用 `<turn id="uuid">` 包裹——直接看到 turn_id，不需要顺序标签映射：

```python
def _serialize_for_summary(messages: list[AnyMessage]) -> str:
    lines: list[str] = []
    current_turn_id: str | None = None
    for msg in messages:
        tid = getattr(msg, "additional_kwargs", {}).get("turn_id", "")
        if tid != current_turn_id:
            # turn 边界——关闭上一个，开新的
            if current_turn_id is not None:
                lines.append("</turn>")
            current_turn_id = tid
            if tid:
                lines.append(f'<turn id="{tid}">')
            else:
                lines.append("<turn>")  # 旧消息无 turn_id
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if is_workspace_notice(msg) or is_system_reminder(msg):
            lines.append(f"[System notice]: {content[:500]}")
        elif isinstance(msg, HumanMessage):
            text = content[:2000] if len(content) > 2000 else content
            lines.append(f"[User]: {text}")
        elif isinstance(msg, AIMessage):
            if content.strip():
                lines.append(f"[Assistant]: {content[:2000]}")
            for tc in getattr(msg, "tool_calls", []) or []:
                # ... tool call 格式化（同现有逻辑）...
        elif isinstance(msg, ToolMessage):
            lines.append(f"[Tool]: {content[:500]}")
    if current_turn_id is not None:
        lines.append("</turn>")
    return "\n".join(lines)
```

LLM 看到：
```
<turn id="abc123def456">
[User]: 帮我修内存泄漏
[Assistant]: 我来用 tracemalloc 排查
[Tool]: tracemalloc 结果显示 cursor 持有 page 引用
</turn>
<turn id="def789abc012">
[User]: 跑测试看看
[Assistant]: 测试通过，RSS 稳定在 380MB
</turn>
```

LLM 产 `turn_index: [{turn_id: "abc123def456", brief: "排查 cursor 内存泄漏"}, {turn_id: "def789abc012", brief: "跑测试验证修复"}]`——**直接用 turn_id，不需要顺序标签映射**。

### 1.2 SummaryDoc 加 `turn_index` 字段（瞬态，不 carry forward）

**文件**：`agent/middlewares/summarization/summary_doc.py`

```python
class TurnBrief(BaseModel):
    """One per-turn brief produced at compaction time."""
    turn_id: str = Field(
        description='The turn_id from the <turn id="..."> tag in <conversation>. Copy it verbatim.'
    )
    brief: str = Field(description="One-sentence summary of what happened in this turn.")

# 加到 SummaryDoc:
turn_index: list[TurnBrief] = Field(
    default_factory=list,
    description=(
        "Per-turn brief summaries of the <conversation> being compacted. "
        'One entry per <turn id="..."> block. '
        "Copy turn_id verbatim from the tag. "
        "If you cannot summarize a turn, leave it out — the pipeline fills it. "
        "FRESH output each compaction — do NOT carry forward old turn_index entries."
    ),
)
```

**不加到** `_LIST_FIELDS` / `_FIELD_CAPS` / `_ITEM_TOKEN_BUDGETS`——独立 validator + `_finalize` 内消费（详见 §1.3）。

**独立 validator**（处理嵌套 dict → `TurnBrief` + json_repair fallback）：

```python
@field_validator("turn_index", mode="before")
@classmethod
def _coerce_turn_index(cls, value: object) -> list:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value] if (value.get("turn_id") is not None) else []
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                tid = item.get("turn_id")
                if tid is not None:
                    result.append({"turn_id": str(tid), "brief": str(item.get("brief", ""))})
            elif hasattr(item, "turn_id"):
                result.append(item)
        return result
    return []
```

**不 carry forward**：`_finalize_summary_doc`（`summary_generation.py:666-722`）末尾清空 + 消费（详见 §1.3）。

**不渲染**：`render_summary_markdown`（`:247-320`）不渲染 `turn_index`——它只进 transcript 的 `<summary>` 块，`turn_index` 只在压缩时被代码消费。

**不计入 SummaryDoc 总字数限制**：总预算检查在 `:845` 只查 `markdown = render_summary_markdown(summary)`，`turn_index` 不被渲染——天然排除。

### 1.3 `_finalize_summary_doc` 内消费 `turn_index`：校验 + 代码兜底 + 截断 + 排序 + 写表 + 清空

**文件**：`agent/middlewares/summarization/summary_generation.py`

`_finalize_summary_doc`（`:666-722`）已接收 `messages`（= `messages_to_summarize`）和 `session_id`。`messages` 每条带 `additional_kwargs["turn_id"]`（基础设施 stamp）。

**包裹 + 代码兜底**：`<turn id="turn_id">` 包裹让 LLM 直接看到 turn_id 并填进 `turn_index`（不需要 `turn_seq → turn_id` 映射）。代码兜底处理 LLM 不产或漏产——用第一条 user 消息截断作 brief。

```python
def _validate_and_consume_turn_index(
    turn_index: list[TurnBrief],
    messages: Sequence[AnyMessage],
    session_id: str,
) -> None:
    """在 _finalize_summary_doc 内调：校验 + 代码兜底 + 截断 + 排序 + 写表。

    LLM 通过 <turn id="..."> 包裹直接看到 turn_id 并填进 turn_index。
    本函数校验 LLM 的 turn_id 是否在实际集合里，漏了的用代码兜底。

    保证 1:1 对应 + 信息量排序 + cap 15：
    1. 从 messages 提取实际 turn_id 集合 + 每轮总 token 数 + 每轮第一条 user 消息（代码兜底用）
    2. LLM 产出按 turn_id dedup（最后一个赢），丢弃不在实际集合里的（幻觉）
    3. 每个 turn_id 一个 brief——LLM 产了的用 LLM 的，没产的用代码兜底（第一条 user 消息截断）
    4. 每个 brief 用 truncate_by_tokens 截断（CJK 感知 head/tail，§1.4）
    5. 按 conversation turn 总 token 数降序排列——信息量最大排最前
    6. cap 到 turn_index_max_items（默认 15），超的丢弃
    7. 写 turn_summaries 表（按 turn_id，best-effort）
    """
    # 1. 提取实际 turn_id + token 数 + 第一条 user 消息（代码兜底）
    actual_turn_ids: set[str] = set()
    turn_token_counts: dict[str, int] = {}
    turn_first_user_msg: dict[str, str] = {}  # turn_id → 第一条 user 消息
    for msg in messages:
        tid = getattr(msg, "additional_kwargs", {}).get("turn_id", "")
        if not tid:
            continue
        actual_turn_ids.add(tid)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        turn_token_counts[tid] = turn_token_counts.get(tid, 0) + estimate_text_tokens(content)
        if tid not in turn_first_user_msg and isinstance(msg, HumanMessage):
            turn_first_user_msg[tid] = content[:200]

    if not actual_turn_ids:
        return  # 旧消息（stamp 前），无 turn_id → 不写表

    # 2. LLM 产出按 turn_id dedup，丢弃不在实际集合里的（幻觉/抄错）
    budget = SUMMARIZATION["turn_index_brief_max_tokens"]
    llm_briefs: dict[str, str] = {}  # turn_id → brief
    for entry in turn_index:
        tid = str(entry.turn_id)
        if tid in actual_turn_ids:
            llm_briefs[tid] = entry.brief

    # 3. 每个 turn_id 一个 brief——LLM 产了的用 LLM 的，没产的用代码兜底
    validated: list[dict] = []  # [{turn_id, brief, total_tokens, source}]
    for tid in actual_turn_ids:
        brief = llm_briefs.get(tid)
        source = "llm"
        if brief is None:
            # ★ 代码兜底：用第一条 user 消息截断作 brief
            user_msg = turn_first_user_msg.get(tid, "")
            brief = f"[code-generated] {user_msg}" if user_msg else f"[turn {tid[:8]}] (no content)"
            source = "code"
        # 4. CJK 感知 head/tail 截断
        brief = truncate_by_tokens(brief, budget)
        validated.append({
            "turn_id": tid,
            "brief": brief,
            "total_tokens": turn_token_counts[tid],
            "source": source,
        })

    # 5. 按信息量降序——信息量最大排最前
    validated.sort(key=lambda e: e["total_tokens"], reverse=True)

    # 6. cap 到 15，超的丢弃
    max_items = SUMMARIZATION["turn_index_max_items"]
    if max_items > 0 and len(validated) > max_items:
        validated = validated[:max_items]

    # 7. 写表 + embeddings（best-effort）
    try:
        from context_engine.store import turn_summary_repo
        turn_summary_repo.upsert_turn_summaries(session_id, validated)
        from context_engine.embeddings.indexer import index_turn_summary
        for entry in validated:
            index_turn_summary(session_id, entry["turn_id"], entry["brief"])
    except Exception as e:
        logger.debug("[summarization] turn_summaries write/embed failed: {}", e)
```

**代码兜底的 brief 格式**：`[code-generated] 帮我修内存泄漏`——用第一条 user 消息截断。不是语义摘要（"排查 cursor 内存泄漏"）但够 FTS 搜索用（关键词"内存泄漏"能命中）。`source` 字段标 `"code"` / `"llm"` 方便调试——知道哪些 brief 是代码产的。`get_turn_messages` 取回原始消息后模型能看到完整内容——brief 只是搜索索引，不是最终内容。

**在 `_finalize_summary_doc` 末尾加**（`:720` `if not updates: return doc` 之前）：

```python
# 消费 turn_index：校验 + 代码兜底 + 写表（best-effort），然后无条件清空
if session_id and doc.turn_index:
    try:
        _validate_and_consume_turn_index(doc.turn_index, messages, session_id)
    except Exception:
        logger.debug("[summarization] _validate_and_consume_turn_index failed, still stripping turn_index", exc_info=True)
updates["turn_index"] = []  # ★ 无条件——在 try/except 之外
```

#### 1.3.1 防御性深度：`_build_structured_summary_prompt` 也清空 prior 的 `turn_index`

```python
# _build_structured_summary_prompt 内（summary_generation.py:610-617）
if previous_doc is not None:
    prior_doc = previous_doc.model_copy(update={"turn_index": []})  # 防御性清空
    prior_json = json.dumps(prior_doc.model_dump(), ensure_ascii=False, separators=(",", ":"))
    parts.append(f"<prior-summary-json>\n{prior_json}\n</prior-summary-json>")
```

#### 1.3.2 不变量：per-turn brief 不参与下一轮压缩

四层防御保证 brief 不积累：

| 防御层 | 位置 | 机制 |
|---|---|---|
| ① `_finalize` 无条件清空 | `_finalize_summary_doc` 末尾 | `updates["turn_index"] = []` 在 try/except 之外 |
| ② prompt 注入再清空 | `_build_structured_summary_prompt:610-617` | `model_copy(update={"turn_index": []})` |
| ③ markdown 不渲染 | `render_summary_markdown:247-320` | `turn_index` 不在渲染 section 列表 |
| ④ 独立表 | `turn_summaries` vs `messages` | brief 在独立表，压缩 slice 不含 brief |

### 1.4 per-brief 截断：`truncate_by_tokens` 已内置 head/tail + CJK

`truncate_by_tokens`（`pub/func/message/token_truncation.py:48-77`）已做 60/40 head/tail + CJK 感知 + 幂等。直接调 `truncate_by_tokens(brief, budget)`——不需要新函数。

- **CJK 感知**：`is_cjk_codepoint` + `chars_per_token_cjk`，50 token ≈ 50 中文字符 / 200 英文字符
- **head/tail 截断**：`head_ratio=0.6` + `tail_ratio=0.4`，中间 `...[truncated N chars]...` 省略
- **幂等**：先查 `estimate_text_tokens(text) <= max_tokens`

### 1.5 新 `turn_summaries` 表 + FTS5 + embeddings

**文件**：`context_engine/store/db.py::build_schema_v1` + `_HEAL_AUX_TABLE_DDL`

```sql
CREATE TABLE IF NOT EXISTS turn_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,           -- per-conversation-turn uuid（messages.turn_id 的值）
    brief TEXT NOT NULL,
    token_count INTEGER,             -- 该对话轮原始消息总 token 数（信息量度量）
    created_at TEXT NOT NULL,
    UNIQUE(session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_summaries_session ON turn_summaries(session_id, turn_id);

CREATE VIRTUAL TABLE IF NOT EXISTS turn_summaries_fts USING fts5(brief);
-- triggers 镜像 messages_fts 模式（db.py:229-274）

CREATE TABLE IF NOT EXISTS turn_summary_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_summary_embeddings_session ON turn_summary_embeddings(session_id);
```

`UNIQUE(session_id, turn_id)` —— 一轮一行。重新压缩时 UPSERT。

**新文件 `context_engine/store/turn_summary_repo.py`**：

```python
def upsert_turn_summaries(session_id: str, entries: list[dict]) -> int:
    """UPSERT per-conversation-turn briefs. entries: [{turn_id, brief, total_tokens}]."""
    db = _shared_db()
    now = datetime.now().isoformat()
    for entry in entries:
        db.execute(
            "INSERT OR REPLACE INTO turn_summaries (session_id, turn_id, brief, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, entry["turn_id"], entry["brief"], entry["total_tokens"], now),
        )
    return len(entries)

def search_turn_summaries_fts(session_id: str, query: str, limit: int = 10) -> list[dict]:
    """FTS5 搜索 turn_summaries_fts，返回 [{turn_id, brief, snippet}]。"""
    sql = """
        SELECT ts.turn_id, ts.brief,
               snippet(turn_summaries_fts, 0, '>>>', '<<<', '...', 40) AS snippet
        FROM turn_summaries_fts JOIN turn_summaries ts ON ts.rowid = turn_summaries_fts.rowid
        WHERE ts.session_id = ? AND turn_summaries_fts MATCH ?
        ORDER BY rank LIMIT ?
    """
    ...

def search_turn_summaries_semantic(session_id: str, query_embedding, limit: int = 10) -> list[dict]:
    """语义搜索 turn_summary_embeddings，返回 [{turn_id, brief, score}]。"""
    ...
```

### 1.6 embeddings 索引

**文件**：`context_engine/embeddings/indexer.py`

```python
def index_turn_summary(session_id: str, turn_id: str, brief: str) -> None:
    """为一条 turn_summary 产 embedding 并写入。复用现有 bge-m3 embed model。"""
    embedding = _embed_text(brief)
    db = _shared_db()
    db.execute(
        "INSERT OR REPLACE INTO turn_summary_embeddings "
        "(session_id, turn_id, embedding, model, dim, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, turn_id, embedding, "bge-m3", 1024, datetime.now().isoformat()),
    )
```

在 `_validate_and_consume_turn_index` 内同步调（§1.3 步骤 8），15 个 turn ≈ 0.5-1 秒。可接受（压缩已在等 LLM call）。

### 1.7 扩展 `message_search`：加 `turn_id` 参数 + `search_turns` 模式 + fetch 模式

**文件**：`agent/tools/message_search.py`

`MessageSearchSchema` 加两个字段：

```python
class MessageSearchSchema(BaseModel):
    query: str | None = Field(default=None, description="Search query. Omit to browse recent sessions. If turn_id is set, ignored (fetch mode).")
    turn_id: str | None = Field(default=None,
        description="If set, fetch the full conversation turn (all messages including compacted ones) by turn_id. "
                    "Skips search — direct fetch. Use after a search_turns search returned a turn_id.")
    search_turns: bool = Field(default=False,
        description="Search per-turn briefs of compacted conversation. Returns (turn_id, brief) pairs.")
    semantic: bool = Field(default=False)
    role_filter: str | None = Field(default=None)
    limit: int = Field(default=3)
```

dispatch 逻辑：

```python
def _message_search_tool(query, turn_id, search_turns, semantic, role_filter, limit, session_id):
    if turn_id is not None:
        # ★ fetch 模式：按 turn_id 取回完整对话轮
        return _fetch_turn_by_id(session_id, turn_id)
    if search_turns:
        # ★ turn brief 搜索模式
        return _search_turn_summaries(query, semantic, session_id, limit)
    if query is None:
        # browse 模式（现有）
        return _recent_sessions(session_id, limit)
    # 默认 FTS 搜索模式（现有）
    return _search_messages(query, role_filter, semantic, session_id, limit)
```

**fetch 模式**：

```python
def _fetch_turn_by_id(session_id: str, turn_id: str) -> str:
    db = _shared_db()
    # 先查 turn_summaries 拿 brief（如果有）
    ts_row = db.execute(
        "SELECT brief FROM turn_summaries WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id),
    ).fetchone()
    # 按 turn_id fetch 所有消息（包括 compacted=1）
    rows = db.execute(
        "SELECT role, content, tool_name, tool_calls, turn_num "
        "FROM messages WHERE session_id = ? AND turn_id = ? "
        "ORDER BY turn_num ASC, id ASC",
        (session_id, turn_id),
    ).fetchall()
    if not rows:
        return json.dumps({"success": False, "error": f"turn_id {turn_id} not found"}, ensure_ascii=False)
    lines = []
    for r in rows:
        role = r["role"] or "?"
        content = str(r["content"] or "")[:2000]
        lines.append(f"[{role}]: {content}")
    return json.dumps({
        "success": True, "turn_id": turn_id,
        "brief": ts_row["brief"] if ts_row else None,
        "messages": lines, "count": len(lines),
    }, ensure_ascii=False)
```

**turn brief 搜索模式**：

```python
def _search_turn_summaries(query: str, semantic: bool, session_id: str, limit: int) -> str:
    from context_engine.store import turn_summary_repo
    if semantic:
        results = turn_summary_repo.search_turn_summaries_semantic(session_id, _embed_query(query), limit)
        return json.dumps({"success": True, "mode": "turn_summaries_semantic",
            "results": [{"turn_id": r["turn_id"], "brief": r["brief"], "score": r["score"]} for r in results],
            "count": len(results)}, ensure_ascii=False)
    else:
        results = turn_summary_repo.search_turn_summaries_fts(session_id, query, limit)
        return json.dumps({"success": True, "mode": "turn_summaries_fts",
            "results": [{"turn_id": r["turn_id"], "brief": r["brief"], "snippet": r["snippet"]} for r in results],
            "count": len(results)}, ensure_ascii=False)
```

**agent 的两步回召流程**：

```
agent: message_search(query="内存泄漏", search_turns=True)
  → [{turn_id: "abc123", brief: "排查 cursor 内存泄漏", snippet: "..."}]

agent: message_search(turn_id="abc123")
  → {turn_id: "abc123", brief: "排查 cursor 内存泄漏", messages: ["[User]: ...", ...], count: 5}
```

同一工具，两次调用。不新增独立工具。

### 1.8 UPDATE prompt + JSON rules

**文件**：`agent/middlewares/summarization/summary_generation.py:177-192`

`_SUMMARY_UPDATE_INSTRUCTIONS_STRUCTURED` 加一段：

```python
# 在现有 instructions 末尾加
'- For each <turn id="..."> block in <conversation>, produce a turn_index entry.\n'
'  Copy the turn_id verbatim from the <turn id="..."> tag. Write a one-sentence brief.\n'
'  If you cannot summarize a turn, leave it out — the pipeline fills it.\n'
'  Produce FRESH entries for this <conversation> only — do not carry forward old entries.\n'
```

`_SUMMARY_JSON_RULES`（`:148+`）加：

```python
'  "turn_index": object[] — per-turn brief summaries;\n'
'    each entry is {"turn_id": string (copy from <turn id="...">), "brief": string — one sentence};\n'
'    leave out turns you cannot summarize — pipeline fills them; [] when no <turn> blocks visible;\n'
```

### 1.9 配置

**文件**：`config/features/agent_side/summarization.py`

```python
turn_index_max_items: int           # 信息量最大的 15 条，超的丢弃
turn_index_brief_max_tokens: int    # 每个 brief 的 token 上限（head/tail 截断阈值）
```

```python
"turn_index_max_items": 15,
"turn_index_brief_max_tokens": 50,  # ≈200 字符；超过时 truncate_by_tokens head/tail 截断
```

---

## 2. 关键文件清单

| 文件 | 改动 |
|---|---|
| `config/features/agent_side/summarization.py` | 加 `turn_index_max_items` / `turn_index_brief_max_tokens` |
| `context_engine/store/db.py` | `build_schema_v1` 加 `turn_summaries` + `turn_summaries_fts` + triggers + `turn_summary_embeddings`；`_HEAL_AUX_TABLE_DDL` 加条目 |
| `context_engine/store/turn_summary_repo.py`（新） | `upsert_turn_summaries` / `search_turn_summaries_fts` / `search_turn_summaries_semantic` |
| `context_engine/embeddings/indexer.py` | 加 `index_turn_summary` |
| `agent/middlewares/summarization/summary_doc.py` | `SummaryDoc` 加 `turn_index: list[TurnBrief]` + `TurnBrief` model；独立 `_coerce_turn_index` validator |
| `agent/middlewares/summarization/summary_generation.py` | `_serialize_for_summary` 用 `<turn id="...">` 包裹；UPDATE prompt + JSON rules 加 `turn_index` 说明（含"leave it out — pipeline fills it"）；`_finalize_summary_doc` 内调 `_validate_and_consume_turn_index`（校验 turn_id + 代码兜底 + `truncate_by_tokens` 截断 + 写表 + 清空） |
| `agent/tools/message_search.py` | `MessageSearchSchema` 加 `turn_id` + `search_turns` 参数；dispatch 加 fetch 模式 + turn brief 搜索模式 |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| `truncate_by_tokens` | `pub/func/message/token_truncation.py:48` | brief 截断（CJK head/tail 已内置） |
| `messages_fts` trigger 模式 | `db.py:229-274` | `turn_summaries_fts` triggers 镜像 |
| `message_embeddings` schema | `db.py:276-287` | `turn_summary_embeddings` 镜像 |
| `_HEAL_AUX_TABLE_DDL` 自愈 | `db.py:456-467` | 新表加到这里 |
| `message_search` 多模式 dispatch | `message_search.py:711-752` | 加 fetch + search_turns 两个模式分支 |

### 不引入的依赖

- 不新增独立工具（`get_turn_messages` 合并到 `message_search`）
- 不引入 `split_into_turns`（`turn_id` 分组替代）
- 不引入 `turn_num_end` 列（`WHERE turn_id = ?` 替代 BETWEEN range）
- 不改 `messages` 表 schema（基础设施计划已加 `turn_id` 列）

---

## 3. 测试

| 测试 | 场景 |
|---|---|
| `test_serialize_for_summary_turn_wrapping` | 3 条消息同 `turn_id` + 2 条另一个 `turn_id` → 序列化后前 3 条在 `<turn id="abc">` 内，后 2 条在 `<turn id="def">` 内 |
| `test_serialize_for_summary_no_turn_id` | 消息无 `additional_kwargs["turn_id"]`（旧消息）→ 用 `<turn>` 包裹（无 id 属性，不崩） |
| `test_summary_doc_turn_index_field` | SummaryDoc 接受 `turn_index: list[TurnBrief]`；`TurnBrief` 验证 `turn_id: str` + `brief: str` |
| `test_coerce_turn_index_malformed` | `None` → `[]`；单 dict → `[dict]`；缺 `turn_id` → 跳过 |
| `test_validate_llm_brief_used` | LLM 产了 `turn_id="abc"` brief → 该 turn 的 brief = LLM 产的，source="llm" |
| `test_validate_code_fallback_when_llm_skips` | 2 个 turn_id，LLM 只产 1 个 → 缺的用第一条 user 消息截断作 brief，source="code"，含 `[code-generated]` 前缀 |
| `test_validate_code_fallback_when_llm_empty` | LLM 产 `turn_index=[]` → 所有 turn 用代码兜底 |
| `test_validate_hallucinated_turn_id_dropped` | LLM 产 `turn_id="xyz999"`（不在实际集合）→ 丢弃，代码兜底填 |
| `test_validate_dedup_turn_id` | LLM 产两个 `turn_id="abc"` → dedup last wins |
| `test_validate_sorted_by_total_tokens` | turn 1 总 500 token，turn 2 总 2000 token → 排序后 turn 2 排最前 |
| `test_validate_cap_15` | 20 个 conversation turn，cap 15 → 留信息量最大的 15 个，最小的 5 个丢弃 |
| `test_validate_brief_truncated_cjk` | 超长 CJK brief → `truncate_by_tokens(brief, 50)` head/tail 截断（~50 中文字符） |
| `test_validate_brief_truncated_ascii` | 超长 ASCII brief → 截断 head + marker + tail（~200 字符） |
| `test_validate_brief_truncate_idempotent` | 已含 `...[truncated ` marker → 不再二次截断 |
| `test_validate_no_turn_ids_in_messages` | messages 无 `turn_id`（基础设施未落地）→ 不写表，不崩 |
| `test_validate_write_fail_safe` | mock `upsert_turn_summaries` 抛 → `_finalize` 不崩，doc 仍清空 `turn_index` |
| `test_finalize_strips_turn_index` | `_finalize_summary_doc` 后 `doc.turn_index == []` |
| `test_finalize_strips_even_on_exception` | mock `_validate_and_consume_turn_index` 抛异常 → `_finalize` 仍清空 `turn_index`（不 carry forward） |
| `test_prior_summary_json_excludes_turn_index` | `_build_structured_summary_prompt` 注入的 `<prior-summary-json>` 里 `turn_index` 是 `[]`（即使 previous_doc 有 turn_index） |
| `test_brief_not_in_next_compression_slice` | 压缩后 `messages` 表的 slice（`messages[:cutoff]`）不含 `turn_summaries` 表的 brief 内容——brief 在独立表里 |
| `test_render_excludes_turn_index` | `render_summary_markdown` 输出不含 `turn_index` |
| `test_total_budget_excludes_turn_index` | `<summary>` markdown token 数不含 `turn_index` |
| `test_turn_summaries_upsert` | 同一 `(session_id, turn_id)` 两次写 → UPSERT，一行，brief 是最新值 |
| `test_turn_summaries_fts_search` | 写入后 FTS 查 `brief` 命中，返回 `turn_id` + `snippet` |
| `test_turn_summaries_semantic_search` | 写入 + 索引 embeddings 后语义查询返回 `turn_id` + `score` |
| `test_message_search_fetch_by_turn_id` | `message_search(turn_id="abc123")` → `WHERE turn_id = ?` fetch 该 turn 全部消息（包括 compacted=1），返回 `{turn_id, messages, count}` |
| `test_message_search_search_turns_fts` | `message_search(query="内存泄漏", search_turns=True)` → `[{turn_id, brief, snippet}]`，不走 LLM 摘要 |
| `test_message_search_search_turns_semantic` | `message_search(query="...", search_turns=True, semantic=True)` → `[{turn_id, brief, score}]` |
| `test_message_search_search_turns_empty` | session 无 `turn_summaries` → `{results: [], count: 0}` |
| `test_message_search_fetch_not_found` | 不存在的 `turn_id` → `{success: False, error: ...}` |
| `test_message_search_fetch_includes_brief` | `turn_id` 在 `turn_summaries` 表里 → fetch 返回里含 `brief` 字段 |
| `test_message_search_two_step_flow` | `search_turns=True` 返回 `turn_id` → `turn_id=...` fetch → 返回完整消息（同一工具两步） |
| `test_compression_writes_turn_summaries` | 压缩后 `turn_summaries` 有行，`turn_id` == `messages.turn_id` |
| `test_schema_migration_old_db` | 旧 db 首次连接 → `_HEAL_AUX_TABLE_DDL` 自愈建表 |

---

## 4. 优先级与分阶段落地

**P0（基础设施前置）**：
- `TODO/计划_基础设施_stamp_turn_id到messages表.md`——stamp `turn_id` 到 `messages` 表 + `additional_kwargs`

**P1（最小可用路径：模型能搜到 turn_id 并取回原始消息）**：
- §1.5 schema 加 `turn_summaries` + FTS 表
- §1.7 `message_search` 加 `turn_id` + `search_turns` 参数 + fetch/turn-search 两个 dispatch 分支
- §1.2 SummaryDoc 加 `turn_index` 字段 + 独立 validator
- §1.3 `_finalize` 内调 `_validate_and_consume_turn_index`（按 `turn_id` 分组 + 写表 + 清空）
- §1.1 `_serialize_for_summary` 用 `<turn id="...">` 包裹
- §1.8 UPDATE prompt + JSON rules 加 `turn_index` 说明
- §1.9 配置

**P2（健壮性收尾）**：
- §1.6 embeddings 索引 + `search_turn_summaries_semantic`
- `index_pending_turn_summaries` 自愈
- `turn_summaries` 表 prune（复用 `max_total_flows_per_session` 模式）

---

## 5. 已知不在本计划范围内

- **`turn_num` 改动**：不碰——MesMemory 内部 bookkeeping 用。`turn_index` 只用 `turn_id`
- **`split_into_turns`**：不用——`turn_id` 分组替代。基础设施 stamp `turn_id` 后不需要
- **`turn_num_end` 列**：不加——`WHERE turn_id = ?` 替代 BETWEEN range
- **`get_turn_messages` 独立工具**：不新建——合并到 `message_search` 的 `turn_id` fetch 模式
- **`message_search` 默认 FTS 模式改动**：不改——现有 LLM 摘要格式不变。agent 想拿 `turn_id` 用 `search_turns=True`；想搜原始消息用默认模式
- **`compaction_checkpoints` 表改动**：不碰

---

## 6. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| LLM 不产 `turn_index` | 字段默认 `[]`，不产不写表——降级到现有行为，不崩 |
| `turn_index` 让 SummaryDoc 输出 token 膨胀 | `turn_index_max_items=15` + `turn_index_brief_max_tokens=50` 双 cap；15×50=750 token 上限 |
| `turn_summaries` 表无界增长 | 加 `max_turn_summaries_per_session` 配置，sweeper 节流 prune |
| embeddings 索引延迟 | `_validate_and_consume_turn_index` 内同步调（15 个 turn ≈ 0.5-1 秒，压缩已在等 LLM） |
| `message_search` 加两个参数让 schema 复杂 | `turn_id` 和 `search_turns` 都有清晰 description；dispatch 是 if/elif 互斥不交叉 |
| `message_search` fetch 模式返回大 | 每条消息截到 2000 字符（跟 `_serialize_for_summary` 同上限） |
| LLM 产了错误的 `turn_id`（hallucinated / 抄错 uuid） | `_validate_and_consume_turn_index` 用 `actual_turn_ids` 集合校验，不在集合里的丢弃，代码兜底填 |
| LLM 不产 `turn_index`（整个字段空） | **代码兜底**——所有 turn 用第一条 user 消息截断作 brief，source="code" |
| 基础设施未落地时 `turn_id` 为空 | `_validate_and_consume_turn_index` 看到 `not turn_id_to_seq` → return，不写表，不崩 |
| 子代理压缩也写 `turn_summaries` | 子代理 session_id 独立，不冲突；子代理的 turn brief 在不同 session_id 下 |

---

## 7. 数据流总览

```
基础设施（前置）：
  turn_scope 设 current_turn_id = uuid4（per-conversation-turn）
  _persist_batch stamp messages.turn_id = current_turn_id
  _persist_batch stamp additional_kwargs["turn_id"] = current_turn_id

压缩触发：
  │
  ├─ _serialize_for_summary(messages_to_summarize)
  │   → 按 turn_id 分组，用 <turn id="turn_id"> 包裹
  │   → '<turn id="abc123">\n[User]: 帮我修内存泄漏\n[Assistant]: ...\n</turn>'
  │
  ├─ LLM with_structured_output(SummaryDoc) → 产 turn_index: [{turn_id: "abc123", brief: "..."}]
  │   （LLM 直接从 <turn id="..."> 抄 turn_id，不需要映射）
  │
  ├─ _validate_and_consume_turn_index(doc.turn_index, messages, session_id)
  │   → 校验 LLM 的 turn_id 是否在 actual_turn_ids 集合里
  │   → 不在的丢弃（幻觉/抄错），代码兜底填（第一条 user 消息截断）
  │   → truncate_by_tokens 截断 brief（CJK head/tail）
  │   → 按信息量降序排前 15
  │   → 写 turn_summaries 表（key = turn_id uuid）+ embeddings
  │
  ├─ doc.turn_index = []（清空，不 carry forward）
  └─ _finalize_summary_doc(doc) → 写链

搜索回溯（同一工具两步）：
  message_search(query="内存泄漏", search_turns=True)
    → turn_summaries_fts MATCH '内存泄漏'
    → [{turn_id: "abc123", brief: "排查 cursor 内存泄漏", snippet: "..."}]

  message_search(turn_id="abc123")
    → SELECT * FROM messages WHERE turn_id = 'abc123' ORDER BY turn_num ASC, id ASC
    → 返回该 conversation turn 的全部消息（包括 compacted=1）
```

---

## 8. 一句话

压缩 LLM 在现有 pass 里多产一个瞬态 `turn_index` 字段（per-turn brief + `turn_id`），`_serialize_for_summary` 用 `<turn id="turn_id">` 包裹让 LLM 直接看到并抄 turn_id（不需要 `turn_seq → turn_id` 映射）。`_finalize_summary_doc` 内的 `_validate_and_consume_turn_index` 校验 turn_id + **代码兜底**（LLM 不产或漏产的 turn 用第一条 user 消息截断作 brief，source="code"），`truncate_by_tokens` 截断（CJK head/tail），按信息量降序排前 15，写入新 `turn_summaries` 表（FTS5 + embeddings，key = `turn_id`），然后清空不 carry forward（四层防御防积累）。`message_search` 加 `turn_id` + `search_turns` 参数：`search_turns=True` 搜 turn brief 返回 `{turn_id, brief}`；`turn_id=...` 直接 `WHERE turn_id = ?` fetch 完整对话轮。同一工具两步回召，不新增独立工具。前置依赖 `TODO/计划_基础设施_stamp_turn_id到messages表.md`。
