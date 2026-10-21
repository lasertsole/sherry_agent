# 计划：taskflow 幂等键 + step claim 锁 + 工具循环检测

- 建档日期：2026-10-21
- 状态：方案设计，未开工
- 来源：跨项目调研（hermes 三层防重 vs claudecode/codex 裸奔）+ sherry 审计（`messages.idempotency_key` 是消息级不是 taskflow 级）
- 严重性：中（子代理可重复创建相同 flow、多 dispatcher 可同时 dispatch 同一 step、子代理可陷入工具循环——浪费 token + 产生重复副作用）
- 借鉴：hermes-agent 的 `idempotency_key`（`kanban_db.py:2540-2553`）+ `claim_lock` CAS（`:3434-3448`）+ `ToolCallSignature`（`tool_guardrails.py:127-137`）

---

## 0. 问题

### 0.1 sherry 当前完全没有 taskflow 级幂等

| 场景 | 当前行为 | 后果 |
|---|---|---|
| 子代理 A 创建了 flow X，子代理 B 又创建了一个相同目标的 flow | 两个 flow 并行做同一件事 | token 浪费 + 副作用重复（如两次 `write_file` 同一路径） |
| 多个 dispatcher 同时 dispatch 同一个 step | 第二个 `UPDATE status='dispatched'` 覆盖第一个 | step 被重复执行，子代理 spawn 两次做同一件事 |
| 子代理陷入循环重复调用同一工具（如 `read_file` 同一路径） | 无限重复，直到 context 耗尽 | token 浪费 + turn 不收敛 |

### 0.2 现有 `messages.idempotency_key` 不是 taskflow 级

`messages.idempotency_key`（`store/core.py:432`）格式 `f"{session_id}:{turn_num}:{turn_ms}:{index}:{digest}"`——per-message persist 去重（防同一消息重复写入 `messages` 表），**不是** taskflow 创建去重。`task_flows` 表没有 `idempotency_key` 列。

### 0.3 跨项目对照

| 机制 | hermes | claudecode | codex | sherry |
|---|---|---|---|---|
| 任务创建去重 | ✓ `idempotency_key` | ✗ | ✗ | ✗ |
| 任务 claim 锁 | ✓ `claim_lock` CAS | ✗ | ✗ | ✗ |
| 工具循环检测 | ✓ `ToolCallSignature` | ✗ | ✗ | ✗ |
| 结果缓存 | ✗ | ✗ | ✗ | ✗ |

sherry 加三层后跟 hermes 对齐——三个同级项目中只有 hermes 有这些机制，sherry 做了之后比 claudecode/codex 更完善。

---

## 1. 设计

### 1.1 `taskflow_create` 加 `idempotency_key`——创建去重

**文件**：`agent/tools/taskflow/tools/taskflow_create.py` + `agent/tools/taskflow/registry/store_sqlite.py`

**借鉴 hermes**：`create_task` 接受 `idempotency_key`，创建前查重——已存在就返回已有 flow_id，不重复创建（`kanban_db.py:2540-2553`）。

#### 1.1.1 schema 加列

`task_flows` 表加 `idempotency_key TEXT` 列 + 唯一索引：

```sql
-- build_schema_v1 里加列（跟 turn_id/terminal_reason 同 ALTER 模式）
ALTER TABLE task_flows ADD COLUMN idempotency_key TEXT

-- 唯一索引：同一 session 内 idempotency_key 唯一（跨 session 不唯一——不同 session 可能合理地创建相同 key 的 flow）
CREATE UNIQUE INDEX IF NOT EXISTS idx_taskflow_idempotency
ON task_flows(session_id, idempotency_key) WHERE idempotency_key IS NOT NULL
```

partial unique index——`NULL` 的行不参与唯一约束（没传 key 的 flow 不去重）。

#### 1.1.2 `taskflow_create` 工具加参数

```python
class TaskflowCreateSchema(BaseModel):
    # ... 现有参数 ...
    idempotency_key: str | None = Field(
        default=None,
        description=(
            "Caller-supplied dedup key. If a non-terminal flow with the same "
            "key already exists in this session, returns the existing flow_id "
            "instead of creating a duplicate. Use when spawning subagents that "
            "might retry creation — prevents duplicate flows. "
            "A UUID or content hash is recommended. None = no dedup."
        ),
    )

@tool("taskflow_create", args_schema=TaskflowCreateSchema)
def _taskflow_create_tool(
    # ... 现有参数 ...
    idempotency_key: str | None = None,
    todo_content: str = "",  # 入口防御：缺省回退 description/flow id，绑定流级 GC root
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    # 1. 幂等查重——在创建前查
    if idempotency_key:
        existing = await store.get_flow_by_idempotency_key(session_id, idempotency_key)
        if existing is not None:
            return (
                f"Flow already exists (idempotency_key={idempotency_key}): "
                f"flow_id={existing.flow_id}, status={existing.status}. "
                f"Use taskflow_resume to continue or taskflow_summary to inspect."
            )

    # 2. 创建 flow（现有逻辑 + idempotency_key 写入行）
    flow = await store.create_flow(session_id=session_id, idempotency_key=idempotency_key, ...)

    # 3. 自动建 todo（流级 GC root，已随入口防御落地）
    ...
```

#### 1.1.3 `get_flow_by_idempotency_key` 查询

```python
async def get_flow_by_idempotency_key(session_id: str, key: str) -> dict | None:
    """查非终态 flow（终态的允许重建——任务已结束，可以重新创建续接）。
    跟 hermes create_task 的 `status != 'archived'` 同语义。
    """
    row = _shared_db().execute(
        f"SELECT flow_id, status FROM {TABLE_NAME} "
        f"WHERE session_id = ? AND idempotency_key = ? "
        f"AND status NOT IN ({','.join('?' * len(TERMINAL_STATUSES)}) "
        f"ORDER BY created_at DESC LIMIT 1",
        (session_id, key, *TERMINAL_STATUSES),
    ).fetchone()
    return dict(row) if row else None
```

**为什么只查非终态**：终态 flow（done/failed/cancelled）的 idempotency_key 不阻止重建——任务已结束，可以创建新 flow 续接（`resumed_from` 指向旧 flow）。只有还在 running/waiting 的 flow 才阻止重复创建。

#### 1.1.4 `INSERT OR IGNORE` + 检查 `rowcount`

hermes 的 `create_task` 在 write_txn 外查重（best-effort race）。sherry 用 unique index 更强——`INSERT OR IGNORE` + 检查是否真的插入了：

```python
# store_sqlite.py::create_flow
cursor = db.execute(
    f"INSERT OR IGNORE INTO {TABLE_NAME} (..., idempotency_key, ...) VALUES (...)",
    (..., idempotency_key, ...),
)
if cursor.rowcount == 0 and idempotency_key is not None:
    # unique 约束冲突——已有同 key 的行，查回来返回
    return await get_flow_by_idempotency_key(session_id, idempotency_key)
```

`INSERT OR IGNORE` + unique index 保证原子性——即使两个子代理同时创建相同 key 的 flow，只有一个 INSERT 成功，另一个 `rowcount=0` → 查回已有行。比 hermes 的 best-effort race 更强。

### 1.2 step dispatch CAS 锁——防重复 dispatch

**文件**：`agent/tools/taskflow/registry/store_sqlite.py` + `agent/tools/taskflow/tools/taskflow_dispatch.py`

**借鉴 hermes**：`claim_task` 用 `UPDATE WHERE claim_lock IS NULL` CAS——第二个 claimant 拿到 `rowcount != 1` → 拒绝（`kanban_db.py:3434-3448`）。

#### 1.2.1 step `ready → dispatched` 用 CAS

当前 `dispatch_step`（或 `auto_dispatch`）的 step 状态转换可能不是原子的——多个 dispatcher 线程可以同时把同一个 step 从 `ready` 转成 `dispatched`。

改成 CAS：

```python
# store_sqlite.py::mark_step_dispatched（新或改现有）
async def mark_step_dispatched(flow_id: str, step_id: str, *, session_id: str) -> bool:
    """CAS: ready → dispatched。返回 True = 成功 claim，False = 已被别人 claim。

    借鉴 hermes claim_task 的 UPDATE WHERE claim_lock IS NULL 模式。
    """
    cursor = _shared_db().execute(
        f"UPDATE {TABLE_NAME} SET "  # 这是对 state_json 的 UPDATE
        # step status 在 state_json 里——用 json_extract
        f"state_json = json_set(state_json, '$.steps.#' || ? || '.status', 'dispatched'), "
        f"updated_at = ? "
        f"WHERE flow_id = ? AND session_id = ? "
        f"AND json_extract(state_json, '$.steps.#' || ? || '.status') = 'ready'",
        (step_id, time.time(), flow_id, session_id, step_id),
    )
    return cursor.rowcount == 1
```

`WHERE ... AND status = 'ready'` 是 CAS 条件——如果 step 已被另一个 dispatcher 转成 `dispatched`，这个 UPDATE 的 `rowcount = 0` → 返回 `False` → dispatcher 跳过。

#### 1.2.2 dispatcher 拿到 `False` 时跳过

```python
# taskflow_dispatch.py
for step in ready_steps:
    claimed = await store.mark_step_dispatched(flow_id, step.step_id, session_id=session_id)
    if not claimed:
        # 已被另一个 dispatcher claim——跳过，不 spawn 子代理
        logger.debug("[taskflow] step {} already dispatched by another dispatcher", step.step_id)
        continue
    # claim 成功——spawn 子代理
    await spawn_subagent(...)
```

**效果**：多个 dispatcher（如 `auto_dispatch` wave + 手动 `taskflow_dispatch`）同时跑时，同一个 step 只被 claim 一次——只 spawn 一个子代理。

### 1.3 工具循环检测——`ToolCallSignature` 防重复调用

**文件**：`agent/middlewares/tool_guardrails/`（新中间件）或扩展现有 `ToolGuardrails`

**借鉴 hermes**：`ToolCallSignature` = `tool_name + SHA256(canonical_args)`，连续 N 次相同签名 → block（`tool_guardrails.py:127-137, 263-281`）。

#### 1.3.1 `ToolCallSignature` 签名

```python
# agent/middlewares/tool_loop_detection/signature.py（新）
import hashlib
import json

@dataclass(frozen=True)
class ToolCallSignature:
    """Stable identity for a tool call: name + canonical args hash."""
    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: dict) -> "ToolCallSignature":
        canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
        return cls(
            tool_name=tool_name,
            args_hash=hashlib.sha256(canonical.encode()).hexdigest()[:16],
        )
```

#### 1.3.2 循环检测中间件

```python
# agent/middlewares/tool_loop_detection/core.py（新）
from collections import defaultdict

class ToolLoopDetectionMiddleware(AgentMiddleware):
    """检测连续重复工具调用，N 次后 block。

    借鉴 hermes tool_guardrails.py:263-281。
    只 block 不缓存——不返回上次结果，只是不让再跑。
    """

    def __init__(self):
        self._signatures: dict[str, list[ToolCallSignature]] = defaultdict(list)  # session_id → 签名历史
        self._max_repeats = TOOL_LOOP_DETECTION["max_repeats"]  # 默认 5

    async def awrap_tool_call(self, request, handler):
        tool_name = request.name
        args = request.args or {}
        session_id = _extract_session_id(request)
        sig = ToolCallSignature.from_call(tool_name, args)

        history = self._signatures[session_id]
        # 计算连续相同签名的次数
        consecutive = 0
        for prev in reversed(history):
            if prev == sig:
                consecutive += 1
            else:
                break

        if consecutive >= self._max_repeats:
            return ToolMessage(
                content=(
                    f"[Tool loop detected] {tool_name} with same arguments has been "
                    f"called {consecutive} times consecutively. This turn is blocked "
                    f"to prevent waste. Try different arguments or a different tool."
                ),
                tool_call_id=request.tool_call_id,
            )

        # 记录签名 + 执行
        history.append(sig)
        # 保留最近 N 条（防 list 无限增长）
        if len(history) > 100:
            history[:] = history[-100:]
        return await handler(request)
```

#### 1.3.3 区分 read-only 和 mutating

借鉴 hermes 的 `IDEMPOTENT_TOOL_NAMES` / `MUTATING_TOOL_NAMES`（`tool_guardrails.py:20-60`）：

```python
# 只对 read-only 工具做"同签名同结果"的 no-progress 检测
# 对 mutating 工具只做"同签名"的重复检测（不查结果——mutating 工具的结果可能不同即使 args 相同）
READ_ONLY_TOOLS = frozenset({
    "read_file", "search_files", "message_search", "skill_list",
    "skill_view", "taskflow_summary", "taskflow_progress",
    "taskflow_list", "todoread", ...
})

# mutating 工具的连续相同调用更危险（重复 write_file / patch_file / terminal）
# → 阈值更低（默认 2 次，不是 5 次）
MUTATING_TOOLS = frozenset({
    "write_file", "patch_file", "terminal", "python_repl",
    "taskflow_create", "taskflow_finish", "taskflow_fail",
    "taskflow_cancel", "todowrite", ...
})
```

检测逻辑：
- read-only 工具：连续 N 次（默认 5）相同签名 → block
- mutating 工具：连续 M 次（默认 2）相同签名 → block（mutating 更危险，阈值更低）

#### 1.3.4 重置时机

签名历史在以下时机重置：
- **新 turn 开始**（`before_agent`）——每轮 turn 的工具调用历史独立
- **不同工具调用**——一旦调了不同的工具，连续计数归零（`consecutive` 逻辑已在代码里——遇到不同签名 break）

### 1.4 配置

**文件**：`config/features/agent_side/tool_loop_detection.py`（新）或加到现有 guardrails config

```python
class ToolLoopDetectionConfig(TypedDict):
    max_repeats_readonly: int     # read-only 工具连续相同调用阈值，默认 5
    max_repeats_mutating: int     # mutating 工具连续相同调用阈值，默认 2
    history_max_len: int          # 签名历史保留长度，默认 100
    enabled: bool                 # 默认 True

TOOL_LOOP_DETECTION: ToolLoopDetectionConfig = {
    "max_repeats_readonly": 5,
    "max_repeats_mutating": 2,
    "history_max_len": 100,
    "enabled": True,
}
```

### 1.5 中间件注册

`ToolLoopDetectionMiddleware` 加到 `agent/middlewares/catalog.py::MIDDLEWARE_ORDER`，在 `ToolGuardrails` 之后（Guardrails 做权限检查，LoopDetection 做重复检查）。

**不是 required**——可以 per-session 关闭（`AGENT_CONFIG["middlewares_disabled"]` 里加 `ToolLoopDetection`）。

---

## 2. 关键文件清单

| 文件 | 改动 |
|---|---|
| `agent/tools/taskflow/tools/taskflow_create.py` | 加 `idempotency_key` 参数 + 创建前查重（§1.1.2） |
| `agent/tools/taskflow/tools/taskflow_plan.py` | 传 `idempotency_key`（如果有）给 `taskflow_create` |
| `agent/tools/taskflow/registry/store_sqlite.py` | 加 `idempotency_key` 列 + partial unique index；加 `get_flow_by_idempotency_key`；`create_flow` 用 `INSERT OR IGNORE` + rowcount 检查；加 `mark_step_dispatched` CAS（§1.2.1） |
| `agent/tools/taskflow/tools/taskflow_dispatch.py` | dispatch 用 `mark_step_dispatched` CAS，`False` 时跳过（§1.2.2） |
| `agent/middlewares/tool_loop_detection/__init__.py`（新） | `ToolLoopDetectionMiddleware` |
| `agent/middlewares/tool_loop_detection/core.py`（新） | 循环检测逻辑（§1.3.2） |
| `agent/middlewares/tool_loop_detection/signature.py`（新） | `ToolCallSignature`（§1.3.1） |
| `agent/middlewares/catalog.py` | `MIDDLEWARE_ORDER` 加 `ToolLoopDetection`（在 `ToolGuardrails` 之后） |
| `config/features/agent_side/tool_loop_detection.py`（新） | 配置 |
| `config/features/__init__.py` | 导出 `TOOL_LOOP_DETECTION` |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| `ALTER TABLE ADD COLUMN` 自愈 | `store_sqlite.py:107, 113` | `idempotency_key` 列同模式 |
| `INSERT OR IGNORE` | SQLite 原生 | §1.1.4 原子去重 |
| `AgentMiddleware` 基类 | `langchain.agents` | `ToolLoopDetectionMiddleware` 继承 |
| `TERMINAL_STATUSES` | `taskflow/config.py:69-75` | `get_flow_by_idempotency_key` 的 WHERE |
| `MIDDLEWARE_ORDER` 注册 | `agent/middlewares/catalog.py` | 加 `ToolLoopDetection` |

### 不引入的依赖

- 不引入结果缓存（借鉴 hermes 的 `ToolCallSignature` 但不缓存结果——只 block 不 cache）
- 不引入 `claim_lock` 列（用 `state_json` 里的 `status` 字段做 CAS，不加新列）
- 不引入跨 session 去重（`idempotency_key` 的 unique 约束是 per-session）

---

## 3. 测试

| 测试 | 场景 |
|---|---|
| `test_idempotency_key_returns_existing_flow` | 同一 session + 同一 key 第二次 `taskflow_create` → 返回已有 flow_id，不创建新 flow |
| `test_idempotency_key_none_allows_duplicate` | 不传 `idempotency_key` → 两次创建得到两个不同 flow（不去重） |
| `test_idempotency_key_terminal_allows_recreate` | 同 key 的 flow 已 `done` → 可以重建（终态不阻止） |
| `test_idempotency_key_cross_session` | session A 和 session B 用相同 key → 各自独立创建（per-session unique） |
| `test_idempotency_key_concurrent_insert` | 两个子代理同时创建相同 key → `INSERT OR IGNORE` 保证只有一个成功，另一个查回已有行 |
| `test_step_dispatch_cas_success` | step `ready` → `mark_step_dispatched` → 返回 `True`，status 变 `dispatched` |
| `test_step_dispatch_cas_already_dispatched` | step 已 `dispatched` → `mark_step_dispatched` → 返回 `False`，不重复 spawn |
| `test_step_dispatch_cas_concurrent` | 两个 dispatcher 同时 dispatch 同一 step → 只有一个 `True`，另一个 `False` |
| `test_tool_loop_readonly_blocked` | `read_file` 同 args 连续 5 次 → 第 6 次 block，返回 `[Tool loop detected]` |
| `test_tool_loop_mutating_blocked` | `write_file` 同 args 连续 2 次 → 第 3 次 block |
| `test_tool_loop_different_args_not_blocked` | `read_file` 不同 args → 不 block（签名不同） |
| `test_tool_loop_different_tool_resets_counter` | `read_file` 3 次后调 `search_files` → 计数归零，再调 `read_file` 同 args 从 0 开始 |
| `test_tool_loop_new_turn_resets` | 新 turn 开始 → 签名历史清空 |
| `test_tool_loop_disabled` | `TOOL_LOOP_DETECTION["enabled"]=False` → 不检测，工具重复调用不 block |
| `test_tool_loop_history_capped` | 签名历史超 100 条 → 保留最新 100 条 |
| `test_idempotency_key_index_migration` | 旧 db 无 `idempotency_key` 列 → ALTER 自愈加列 + 建 index，不崩 |

---

## 4. 优先级与分阶段落地

**P0（防最危险的重复副作用）**：
- §1.2 step dispatch CAS——防多 dispatcher 同时 dispatch 同一 step（重复 spawn 子代理）
- §1.1 `taskflow_create` idempotency_key——防子代理重复创建相同 flow

**P1（防 token 浪费循环）**：
- §1.3 工具循环检测——防子代理陷入无限重复调用

**P2（健壮性）**：
- read-only vs mutating 区分阈值
- 签名历史 cap
- per-session 开关

---

## 5. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| `idempotency_key` 误阻止合法重建 | 只查非终态 flow——终态的允许重建（`status NOT IN (terminal)`）。LLM 想重建已完成的 flow → 可以（旧 flow 已终态） |
| `INSERT OR IGNORE` 吞其他约束冲突 | `INSERT OR IGNORE` 只忽略 unique 冲突——如果其他列有约束冲突也会被忽略。缓解：INSERT 后检查 `rowcount == 0` 且 `idempotency_key is not None` → 查回已有行；如果 `idempotency_key is None` → 报错（不该 `rowcount=0`） |
| step CAS 误判——step 刚好被 `taskflow_update_steps` 改回 `ready` 后被另一个 dispatcher claim | 这是正确行为——step 从 `dispatched` 改回 `ready` 意味着重新 dispatch，CAS 保证只有一个 dispatcher claim 成功 |
| 工具循环检测误 block 合法操作 | 阈值 5 次（read-only）/ 2 次（mutating）给足回旋；不同 args 不 block；新 turn 重置；可以 per-session 关闭 |
| 工具循环检测对 mutating 工具阈值太低（2 次） | mutating 工具重复调用确实危险（重复 write_file 覆盖、重复 terminal 执行）——2 次后 block 是保守但安全的。LLM 看到 block 消息后应该换 args 或换工具 |
| `idempotency_key` 跨 session 不唯一 | 设计意图——不同 session 可以有相同 key 的 flow（独立 session 独立 flow）。unique 约束是 `(session_id, idempotency_key)` 不是 `idempotency_key` 单列 |
| 签名历史内存泄漏 | per-session dict + cap 100 条 + 新 turn 重置——不会无限增长 |

---

## 6. 已知不在本计划范围内

- **结果缓存**：不引入——借鉴 hermes 的 `ToolCallSignature` 但只 block 不 cache（不返回上次结果）。如果未来要加结果缓存，可以在 block 前返回上次结果而非 block 消息。
- **跨 session 去重**：不做——`idempotency_key` 的 unique 约束是 per-session。跨 session 去重需要全局 registry，复杂度不值得。
- **子代理 spawn 去重**：不做——子代理 spawn 按 prompt 查重需要语义匹配（"相同目标的 prompt"），不是简单的 args hash。`idempotency_key` 是 LLM 手填的显式声明，更可靠。
- **`claim_lock` 列**：不加——用 `state_json` 里的 `status` 字段做 CAS（`WHERE status='ready'`），不加新列。hermes 的 `claim_lock` 是因为 kanban 的 task 有独立的 claim 生命周期（claim → run → release），sherry 的 step 没有 claim 生命周期——`ready → dispatched` 就是 claim。
- **`ToolCallSignature` 跨 turn 持久化**：不做——签名历史 per-turn 内存，不持久化。跨 turn 的循环检测需要查 MesMemory 历史，复杂度不值得。

---

## 7. 数据流

```
taskflow_create 幂等：
  LLM: taskflow_create(steps=..., idempotency_key="fix-memleak", todo_content="...")
    → get_flow_by_idempotency_key(session_id, "fix-memleak")
    → 已存在非终态 flow？→ 返回已有 flow_id，不创建
    → 不存在？→ INSERT OR IGNORE（unique 约束保证原子）→ 创建 flow + todo

step dispatch CAS：
  dispatcher A: mark_step_dispatched(flow, step="step-2") → True（claim 成功）→ spawn 子代理
  dispatcher B: mark_step_dispatched(flow, step="step-2") → False（已被 A claim）→ 跳过

工具循环检测：
  LLM: read_file("a.py") → 签名 S1
  LLM: read_file("a.py") → 签名 S1（连续第 2 次）
  LLM: read_file("a.py") → 签名 S1（连续第 3 次）
  ...
  LLM: read_file("a.py") → 签名 S1（连续第 5 次）→ block，返回 "[Tool loop detected]"
  LLM: read_file("b.py") → 签名 S2（不同签名，计数归零）
```

---

## 8. 一句话

借鉴 hermes-agent 的三层防重：① `taskflow_create` 加 `idempotency_key` 参数 + `task_flows` 表加 partial unique index——`INSERT OR IGNORE` 原子去重，相同 key 的非终态 flow 返回已有 ID 不重建（比 hermes 的 best-effort race 更强）② step dispatch 用 `UPDATE WHERE status='ready'` CAS——多 dispatcher 同时 dispatch 同一 step 只有一个成功，另一个 `rowcount=0` 跳过 ③ `ToolCallSignature`（tool_name + SHA256(canonical_args)）循环检测中间件——read-only 工具连续 5 次相同签名 → block，mutating 工具 2 次 → block。三层互补：创建去重防重复 flow，CAS 防重复 dispatch，循环检测防重复工具调用。不引入结果缓存（只 block 不 cache）、不引入跨 session 去重（per-session unique）、不引入 `claim_lock` 列（用 `state_json.status` 做 CAS）。
