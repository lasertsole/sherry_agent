# 计划：子代理超时后暴露 checkpoint 供调用者读取

- 建档日期：2026-10-15
- 状态：**计划（未开工）**
- 关联文档：
  - `TODO/计划_sessions_spawn暴露wall_clock超时字段.md`（让模型设 per-spawn 超时）
  - `TODO/计划_续跑enforcer加turn级硬上限.md` §8.1（全局默认 `0.0 → 600.0`）
- 触发问题：子代理 wall-clock 超时后，`_execute_subagent` 的 `except TimeoutError`
  拿到的 `result_text` 取决于哪一轮超时：

  | 哪轮超时 | `result_text` | announce 包含 Result？ |
  |---|---|---|
  | 第一 turn | `None`（从未赋值） | 不包含 |
  | 后续 goal-loop turn | 上一轮的 AIMessage 文本 | 包含（截断 4000 字符） |
  | 被 kill | 同上——上一轮的值或 None | 同上 |

  后续 turn 超时**确实返回了部分结果**——goal loop 里 `result_text` 保留了前
  一轮的值。但这个部分结果只是"最后一条 AIMessage 的文本"，不含工具调用历史
  和工具返回结果。模型只看到"I'll now modify the function..."这种文本，看不到
  它调了什么工具、工具返回了什么。而子代理的 checkpoint 里有完整的部分进度
  ——每次模型调用和工具调用后 LangGraph 都写 checkpoint。checkpoint 数据在
  SQLite 里存活（`aclose()` 只关连接不删数据，子代理 cleanup 路径也不调
  `delete_thread_history`），却没有任何 LLM tool 能读它。
- 结论先行：
  1. **不在 `_execute_subagent` 里自动提取**——自动提取只能拿到最后一条
     AIMessage 的文本（和现有 `result_text` 一样的局限），丢掉了工具调用
     历史和中间产物。调用者有任务上下文，自己决定看什么更有价值。
  2. **新增 `sessions_messages` tool**——让主 agent 按子代理的 session key
     读其 checkpoint 里的 message list。token-budgeted 输出，不灌爆上下文。
  4. **checkpoint 清理策略不改**——子代理 cleanup 已经不删 checkpoint（无论
     `cleanup` 设什么），本方案依赖这一现状，不改动它。
  5. **`limit` 可选不必填**——默认 10，clamp 到 `[1, 30]`。token budget
     总共 4000、每条 500 token，最多显 8 条；`limit=30` 取 30 条已经远超
     可显范围，更大的值只会白处理。`limit` 管"取多少条"，token budget
     管"显示多少"。

---

## 1. 现状分析

### 1.1 超时后的完整链路

```
asyncio.wait_for(child_agent.ainvoke(...), timeout=600)
  │
  ├─ 600s 到 → 取消协程 → TimeoutError
  │
  ├─ except TimeoutError:
  │    outcome = RunOutcome(status=TIMEOUT)
  │    result_text = None（第一 turn）或上一轮结果（后续 turn）
  │
  ├─ finally:
  │    _release_child_resources(run, child_agent)
  │      └─ child_checkpointer.aclose()  ← 关连接，数据留在 SQLite
  │    _finalize_child_run(run, outcome, result_text)
  │      └─ 15s grace → complete_subagent_run → announce
  │
  └─ announce delivery:
       content = "Status: timed out\nTask: ...\n（无 Result）"
       metadata = {..., subagent_child_session_key: "agent:main:subagent:<uuid>"}
```

**checkpoint 在哪**：`SRC_DIR/checkpoints/sqlite.db`，按
`thread_id = rand_str_to_int(child_session_key)` 索引。WAL 模式，并发读安全。

### 1.2 checkpoint 存活的证据

| 清理路径 | 是否删 checkpoint | 证据 |
|---------|----------------|------|
| `child_checkpointer.aclose()` | 否 | 只关 aiosqlite 连接，不执行 DELETE |
| `cleanup="delete"` → `delete_subagent_session_for_cleanup` | 否 | 发 `__session_delete__` 信号到 EventBus，但子代理 session key 无 channel 绑定，消息被 drop |
| `cleanup="keep"` | 否 | `resolve_deferred_cleanup_decision` 直接返回 `(False, "cleanup=keep")`，不进 `_finalize_cleanup` |
| `delete_thread_history` | **是** | 但只在 `server/service/session_cleanup_service.py::purge_session`（HTTP `clear_session` 端点）调用，子代理 cleanup 路径不碰它 |

**结论**：子代理的 checkpoint 在超时后天然存活，不需要额外保护。

### 1.3 调用者已有的信息

主 agent 在 spawn 时就拿到了 `child_session_key`：

```python
# sessions_spawn.py:147-151 — tool 返回文本
parts = [f"Subagent spawned: status={result.status}"]
if result.child_session_key:
    parts.append(f"session_key={result.child_session_key}")
```

这段文本在主 agent 的对话历史里。主 agent 知道 session key，但没有 tool 能用它读 checkpoint。

### 1.4 现有工具的能力边界

| 工具 | 能读子代理 session？ | 为什么不能 |
|------|---------------------|------------|
| `message_search` | 不能 | `session_id` 是 `InjectedState("session_id")`，模型无法指向别的 session；schema 没有 `target_session_key` 参数 |
| `read_messages`（state_port） | 能（技术上） | 接受任意 `session_id`，但它是框架层函数，不在 tool catalog 里，LLM 调不到 |
| `sessions_send` | 不能（超时后） | 只能往 RUNNING/INTERRUPTED 的子代理发消息，terminal 的拒收 |
| `subagents_list` | 不能 | 不返回 `child_session_key`，也不返回 messages |
| `sessions_yield` | 不能 | 等子代理完成，不读历史 |

**缺口**：没有任何 LLM-exposed tool 能按 session key 读子代理的 checkpoint messages。

---

## 2. 设计方案

### 2.1 新增 `sessions_messages` tool

新文件 `agent/tools/subagent/tools/sessions_messages.py`：

```python
class SessionsMessagesSchema(BaseModel):
    target_session_key: str = Field(
        description="The session key of the subagent whose messages to read "
        "(returned as session_key= in the sessions_spawn result)."
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Number of recent messages to return (newest first). "
        "Default 10, clamped to [1, 30]. The output is token-budgeted "
        "regardless of this limit — limit controls how many messages to "
        "fetch from the checkpoint, the token budget controls how much "
        "to show.",
    )
    query: str | None = Field(
        default=None,
        description="Optional keyword filter. If set, only messages containing "
        "the keyword (case-insensitive) are returned. If None, all recent "
        "messages up to the limit are returned.",
    )


class SessionsMessagesTool(BaseTool):
    """LLM tool: read a subagent's checkpoint messages by its session key."""

    name: str = "sessions_messages"
    description: str = (
        "Read the message history of a subagent session by its session key. "
        "Use after a timed-out or killed subagent to review its partial work "
        "(tool calls, tool results, AI reasoning) before deciding whether to "
        "continue, re-plan, or report to the user."
    )
    args_schema: type = SessionsMessagesSchema
    session_id: str = ""
```

### 2.2 `_arun` 实现

```python
async def _arun(
    self,
    target_session_key: str,
    limit: int = 20,
    query: str | None = None,
) -> str:
    # 权限检查：只允许读自己 spawn 的子代理
    allowed, reason = check_read_permission(self.session_id, target_session_key)
    if not allowed:
        return f"Cannot read session: {reason}"

    # 从 checkpoint 读 messages
    messages = await _read_child_messages(target_session_key, limit)

    if not messages:
        return f"No messages found for session {target_session_key} " \
               f"(the checkpoint may be empty or the session never ran)."

    # 关键词过滤（可选）
    if query:
        q = query.lower()
        messages = [m for m in messages if q in _msg_text(m).lower()]

    # token-budgeted 渲染
    return _render_messages(messages, target_session_key, limit)
```

### 2.3 `_read_child_messages` 实现

复用 `agent/state_port.py::read_messages` 的能力——它已经能按任意 `session_id`
读 checkpoint：

```python
async def _read_child_messages(
    child_session_key: str, limit: int
) -> list[BaseMessage]:
    """Read the last N messages from a child session's checkpoint.

    The checkpoint survives child_checkpointer.aclose() — the data is in
    SRC_DIR/checkpoints/sqlite.db keyed by rand_str_to_int(child_session_key).
    """
    from agent.state_port import read_messages

    all_messages = await read_messages(session_id=child_session_key)
    # newest-first, bounded by limit
    return list(reversed(all_messages))[:limit]
```

**为什么复用 `read_messages`**：它已经处理了 graph 解析、`aget_state`、
checkpoint 读取的全部细节。子代理和主 agent 共享同一个 SQLite checkpoint 文件
（`SRC_DIR/checkpoints/sqlite.db`），`read_messages` 用 child_session_key 作
thread_id 就能读到子代理的 checkpoint。

### 2.4 权限检查

```python
def check_read_permission(
    caller_session_id: str, target_session_key: str
) -> tuple[bool, str]:
    """True when the caller spawned the target child session."""
    from ..registry.queries import get_run_by_child_session_key
    from ..registry.session_state import normalize_session_key

    requester_key = f"agent:main:session:{caller_session_id}"
    run = get_run_by_child_session_key(target_session_key)
    if run is None:
        return False, f"no run found for session key {target_session_key}"
    if run.spawned_by != normalize_session_key(requester_key):
        return False, "not the spawning session"
    return True, ""
```

**为什么检查 `spawned_by`**：主 agent 只能读自己 spawn 的子代理，不能读别的
session 的子代理。和 `sessions_send` 的 `can_control_run` 同一个检查维度。

### 2.5 token-budgeted 渲染

```python
def _render_messages(
    messages: list[BaseMessage], session_key: str, limit: int
) -> str:
    """Render messages as a text summary, token-budgeted.

    Each message: [role] tool_calls=[...] content=<truncated>
    Total output capped at TOOLS_TIMEOUTS-equivalent token budget
    (reuse truncate_by_tokens).
    """
    from pub.func.message.token_truncation import truncate_by_tokens

    lines = [f"## Subagent session: {session_key}"]
    lines.append(f"Showing {len(messages)} of last messages (limit={limit}):\n")
    for msg in messages:
        role = msg.type  # "human" / "ai" / "tool"
        text = _msg_text(msg)
        # per-message truncation (reuse existing CJK-aware truncation)
        text = truncate_by_tokens(text, max_tokens=500)
        lines.append(f"[{role}] {text}\n")
    return truncate_by_tokens("\n".join(lines), max_tokens=4000)
```

**为什么 token-budgeted**：子代理可能跑了 30 轮迭代，每轮有模型输出 +
工具结果，全量回传会灌爆主 agent 的上下文。`truncate_by_tokens` 是项目已有的
CJK-aware 截断管道（`pub/func/message/token_truncation.py`），每条消息 500 token，
总计 4000 token——和 `read_file` 的 token budget 量级一致。

### 2.6 超时后的 announce 提示（仅 `result_text` 为空时）

修改 `agent/tools/subagent/announce/delivery.py::_deliver_completion_message`，
在 timeout 状态下**仅当 `ctx.result_text` 为空时**追加一行提示：

```python
elif ctx.outcome.status == "timeout":
    status_text = "timed out"
    if not ctx.result_text:
        # Only hint when there's no partial result — later goal-loop
        # turns already carry the previous turn's text as result_text.
        # First-turn timeout = result_text is None = no Result in announce.
        content_parts.append(
            f"Partial work may be available — use sessions_messages"
            f'(target_session_key="{ctx.child_session_key}") to review '
            "what was accomplished before the timeout."
        )
```

**为什么条件化**：后续 goal-loop turn 超时时，announce 已经包含上一轮的
`result_text`（"Result: ..." 段）。再追加"去读 checkpoint"的提示会让模型困惑
——"我已经有结果了，为什么还要去读？"。只在 `result_text` 为空（第一 turn
超时）时才提示。

**不改 system prompt**：tool description 已经说 "Use after a timed-out or
killed subagent to review its partial work"。模型从 tool 描述发现这个工具，
announce 的一行提示是运行时上下文（在 completion message 里），不是 prompt
bloat。不需要在 system prompt 里加额外的使用指导。

### 2.7 catalog 注册

`agent/tools/catalog.py` 的 `TOOL_GROUPS` 里，`sessions_messages` 归入
`subagents` 组（和 `sessions_spawn` / `sessions_yield` / `sessions_send` 同组）。
和该组其他工具一样是 `BULK_ONLY`（select-all / clear-only，不单独开关）。

---

## 3. 完整执行流

```
主 agent 调 sessions_spawn(task=..., run_timeout_seconds=600)
  → 返回 session_key=agent:main:subagent:<uuid>
  → 子代理开始跑

子代理跑了 30 轮迭代 → 600s 到 → asyncio.wait_for 超时
  → TimeoutError → result_text=None（第一 turn 超时）→ RunOutcome(TIMEOUT)
  → _release_child_resources (aclose checkpointer, 数据留在 SQLite)
  → _finalize_child_run (15s grace → complete → announce)

主 agent 收到 announce（result_text 为空 → 追加提示）:
  Status: timed out
  Task: <原始任务>
  Partial work may be available — use sessions_messages(
    target_session_key="agent:main:subagent:<uuid>"
  ) to review what was accomplished before the timeout.

主 agent 调 sessions_messages(
  target_session_key="agent:main:subagent:<uuid>",
  limit=10,
)
  → check_read_permission: caller spawned this child → OK
  → read_messages(session_id=child_session_key)
    → aget_state(config=build_agent_config(child_session_key))
    → 从 SQLite checkpoint 读出 30 轮 messages
  → 取最后 10 条 → token-budgeted 渲染 → 返回文本

主 agent 看到:
  ## Subagent session: agent:main:subagent:<uuid>
  Showing 10 of last messages (limit=10):

  [human] <原始任务>
  [ai] I'll start by reading the file...
  [tool] read_file result: <truncated>
  [ai] Now I'll modify the function...
  [tool] patch_file result: success
  [ai] The test still fails, let me check...
  ...

主 agent 判断: 部分进度有价值 → 继续 spawn 或自己做

---

如果后续 goal-loop turn 超时（第一 turn 成功，第二 turn 超时）:
  → result_text 保留第一 turn 的 AIMessage 文本
  → announce:
       Status: timed out
       Task: <原始任务>
       Result:
       <第一 turn 的最后一条 AIMessage 文本>
  → 不追加提示（result_text 不为空）
  → 主 agent 已有部分结果，可自行决定是否调 sessions_messages 看完整转录
```

---

## 4. 文件清单

### 新增文件（2 个）

| 文件 | 内容 |
|------|------|
| `agent/tools/subagent/tools/sessions_messages.py` | `SessionsMessagesSchema` + `SessionsMessagesTool`（schema、`_arun`、`check_read_permission`、`_read_child_messages`、`_render_messages`、`_msg_text` helper） |
| `tests/agent/tools/subagent/test_sessions_messages.py` | 见下 |

### 修改文件（3 个）

| 文件 | 改动 |
|------|------|
| `agent/tools/subagent/tools/__init__.py` | 导出 `build_sessions_messages_tool` |
| `agent/tools/subagent/tools/sessions_spawn.py` | `SessionsSpawnTool` 不改——session_key 已在返回文本里 |
| `agent/tools/subagent/announce/delivery.py` | `_deliver_completion_message` 的 timeout 分支追加 partial-work 提示行 |
| `agent/tools/catalog.py` | `TOOL_GROUPS["subagents"]` 加 `sessions_messages` |
| `agent/tools/__init__.py` | `_MAIN_TOOLS_BUILDERS` 加 `build_sessions_messages_tool` |

### 测试用例

1. **`test_read_child_messages_after_timeout`** — spawn 一个子代理，mock 超时，
   调 `sessions_messages`，验证返回 checkpoint 里的 messages
2. **`test_permission_denied_for_non_spawner`** — 另一个 session 的 caller 调
   `sessions_messages` 读这个 child → 拒绝
3. **`test_permission_denied_for_unknown_session`** — target_session_key 不存在 → 拒绝
4. **`test_empty_checkpoint_returns_not_found`** — child 从未跑过 → "No messages found"
5. **`test_query_filter`** — 传 `query="error"` 只返回含 "error" 的 messages
6. **`test_token_budget_truncation`** — child 跑了 50 轮，传 `limit=30`，验证输出不超 token 上限
7. **`test_announce_timeout_includes_hint`** — 超时后的 completion message 包含
   "sessions_messages" 提示和 child_session_key

---

## 5. 边界场景

| 场景 | 行为 | 理由 |
|------|------|------|
| 第一 turn 超时 | checkpoint 有几轮迭代（到取消前的）；announce 无 Result + 提示去读；`result_text=None` | LangGraph 在取消前已写 checkpoint；提示只在 `result_text` 为空时出现 |
| 后续 turn 超时 | announce 已含上一轮 `result_text`（截断 4000 字符）；不追加提示 | 模型已经有部分结果，不再重复提示 |
| 被 kill（sessions_kill） | 同超时——`result_text` 是上一轮或 None；有 `result_text` 不提示，没有则提示 | `CancelledError` 走同一个 `finally` 路径 |
| 子代理从未执行（spawn 失败） | "No messages found" | checkpoint 为空 |
| 子代理正常完成 | 也能调 `sessions_messages` 读 | checkpoint 存活，tool 不限于超时场景 |
| server 重启后调 | 能读——checkpoint 在 SQLite 文件里 | WAL 模式，数据持久 |
| `cleanup="delete"` 后调 | 能读——子代理 cleanup 不删 checkpoint | `delete_thread_history` 只走 HTTP `clear_session` |
| `clear_session` HTTP 端点后调 | "No messages found" | `delete_thread_history` 删了 checkpoint |
| 主 agent 上下文被压缩 | announce 里有 session_key，仍能调 | announce 是 HumanMessage，在压缩后的窗口里 |
| 非 spawner 试图读 | 拒绝 "not the spawning session" | `check_read_permission` 检查 `spawned_by` |
| 输出超 token 上限 | `truncate_by_tokens` 截断 | 每条消息 500 token，总计 4000 token |

---

## 6. 不做的事

- **不在 `_execute_subagent` 里自动提取部分结果**——现有机制已经在后续 turn
  超时时返回上一轮的 `result_text`，announce 已经包含它。自动提取（从
  checkpoint 取最后一条 AIMessage）和现有 `result_text` 做的事一样——都是
  只拿最后一条 AIMessage 的文本，丢掉工具调用历史。`sessions_messages`
  tool 的价值是给调用者**完整的 tool-call 转录**，不只是最后的文本。
- **不改 system prompt**——tool description 已经说明使用时机
  ("Use after a timed-out or killed subagent to review its partial work")。
  announce 的一行提示是运行时上下文，不是 prompt bloat。
- **不扩展 `message_search`**——`message_search` 的 FTS 后端
  （`context_engine`）是按 `m.session_id = ?` 搜索的，加 `target_session_key`
  会改变它的语义（从"搜当前 session"变成"搜任意 session"），不如新开一个
  语义清晰的 tool。`sessions_messages` 的定位是"读子代理的 message list"，
  不是"搜索"。
- **不删子代理的 checkpoint**——子代理 cleanup 已经不删 checkpoint（无论
  `cleanup` 设什么），本方案依赖这一现状。如果未来要加 checkpoint 回收
  （防止 SQLite 文件无限增长），应该是一个独立的定期清理任务，不绑在
  cleanup 路径上。
- **不做 checkpoint resume（断点续跑）**——那是更大的设计：从超时的
  checkpoint 恢复子代理执行。本方案只做"读"，不做"恢复执行"。
  `resolve_run_deadline_ms` 已定义但无人调用，是那个独立缺口的一部分。
- **不给 `subagents_list` 加 `child_session_key` 字段**——`sessions_messages`
  的 schema 要求模型显式传 `target_session_key`，模型从 spawn 结果或
  announce 提示里拿到它。`subagents_list` 改不改是独立决策。
- **不限制 `sessions_messages` 只在超时后可用**——正常完成的子代理也能读。
  checkpoint 在正常完成后也存活，调用者可能想回顾子代理做了什么。tool 的
  description 不限制使用时机。
