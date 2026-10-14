# 计划：1.2 taskflow 并行执行

- 建档日期：2026-10-14
- 状态：方案设计，未开工
- 来源：`TODO/对标计划_Zcode可借鉴能力.md` §1.1 + §1.2
- 包含：① taskflow 并行执行（§1.2）+ ② 血缘指针（§1.1，从 journal 重放计划合并）

---

## 0. 现状

taskflow 的 step dispatch 是**顺序串行**的。三个入口都用了相同的 `for sid in ...: await _dispatch.dispatch_child(...)` 模式：

| 入口 | 文件:行 | 说明 |
|---|---|---|
| 批量 dispatch | `agent/tools/taskflow/tools/taskflow_dispatch.py:116-144` | 显式批量 dispatch，docstring 明确说 "Spawns happen SEQUENTIALLY" |
| 自动 dispatch | `agent/tools/taskflow/tools/taskflow_resume.py:430-501` (`_auto_dispatch_ready`) | resume 标记 step done → unlock dependents → dispatch newly_ready |
| 重试 dispatch | `agent/tools/taskflow/tools/taskflow_wait_all.py:143-169` (`_retry_settled_steps`) | retry settled dead children |

每个 `dispatch_child` 调用 `spawn_subagent_direct`，后者 `asyncio.create_task(...)` 创建后台子 agent 并立即返回 child_session_key。**子 agent 一旦 spawn 就并发运行**（SUBAGENT lane 支持 8 并发），但 spawn 调用本身是串行的——`for` 循环里 `await` 等一个 spawn 完成才做下一个。

waves 系统（`waves.py::compute_waves`）按 DAG 深度分层，但只是 `progress_push.py` 的 reporting view——**dispatch 代码不读 wave 输出**。进度面板能显示 "wave 2 有 4 个可并行 step"，但代码仍逐个 spawn。

---

## 1. 必须先修的并发缺陷（Phase 0，硬前置）

不修这些缺陷就并行化 = 必然丢数据。核验来源：2026-10-14 深度代码审计。

### 1.1 缺陷清单

#### 缺陷 1（致命）：`taskflow_resume` 正常完成路径无 conflict retry

- **位置**：`taskflow_resume.py:383-397`
- **现状**：step DONE 的正常路径直接调 `store_sqlite.update_flow(flow_id, revision, ...)`，`except FlowConflictError` 只返回 `conflict_error(exc)` 字符串——**无 retry**
- redispatch 路径（line 366）才用 `update_flow_with_conflict_retry`
- **后果**：两个并行 step 同时完成 → 两个 `taskflow_resume` 并发写同一 flow → 一个赢（revision N→N+1），一个 `FlowConflictError` 无 retry → **输的那个 result 永远丢失**
- `delivery.py::_try_auto_resume_taskflow` (line 404) 无条件 `return True`，error 被 log 但不检查 → 完全静默

#### 缺陷 2（致命）：`_auto_dispatch_ready` 读改写竞态 + 无 conflict retry

- **位置**：`taskflow_resume.py:430-501`
- **现状**：
  - line 451：第一次读 `flow = await store_sqlite.get_flow(flow_id, session_id)`，取 `steps`
  - line 488：第二次读 `latest = await store_sqlite.get_flow(flow_id, session_id)`，取 `expected_revision`
  - line 491：写 `{**dict(latest["state"]), "steps": steps}` —— **steps 是第一次读的旧列表**，期间其他 writer 的修改被覆盖
  - line 497-500：`except Exception: logger.exception(...)` —— **已 spawn 的 child_session_key 未持久化**
- **后果**：两个并行 resume 都 unlock + dispatch → 重复 dispatch 同一 step、丢失已 spawn child 记录、互相覆盖 step 修改

#### 缺陷 3（中）：evidence ledger 无文件锁

- **位置**：`evidence_ledger.py` — `append` 用 `open(path, "a")`，无 `file_write_lock` / `cross_process_lock`
- **现状**：仓库已有 `pub_base/file_lock.py`（flock / msvcrt.locking），但 evidence ledger 没用
- `read_all()` 无 per-line `try/except JSONDecodeError` → partial line 让 `json.loads` 崩 → `collect_evidence_summary` 崩 → `taskflow_resume` 崩 → `_try_auto_resume_taskflow` 静默吞掉
- **后果**：并发追加写 JSONL 在 Windows 上可能产生 partial line → 并发读崩溃

#### 缺陷 4（低）：`_try_auto_resume_taskflow` 无条件返回 True

- **位置**：`delivery.py:366-407`
- `taskflow_resume` 返回 success 字符串或 `Error: ...` 字符串，`_try_auto_resume_taskflow` 都 `return True`
- **后果**：resume 失败（conflict、not found）被完全吞掉，无法追溯

### 1.2 Phase 0 改动

#### 0a：`taskflow_resume` 正常完成路径加 conflict retry

**文件**：`agent/tools/taskflow/tools/taskflow_resume.py`

将 line 383-397 的直接 `update_flow` 调用改为 `update_flow_with_conflict_retry`，和 redispatch 路径（line 366）一致：

```python
# 改前 (line 383-397):
try:
    updated = await store_sqlite.update_flow(
        flow_id, revision, session_id=session_id,
        state={**dict(latest["state"]), "steps": steps, "results": results},
        status="running" if has_unfinished else "done",
    )
except FlowConflictError as exc:
    return conflict_error(exc)

# 改后:
async def _build_done_state(fresh_flow, attempt):
    fresh_steps = fresh_flow["state"]["steps"]
    fresh_results = fresh_flow["state"].get("results", [])
    # 重新应用：标记当前 step DONE + 注入 result + unlock dependents
    by_id = {s["id"]: s for s in fresh_steps}
    if step_id in by_id:
        s = by_id[step_id]
        s["status"] = StepStatus.DONE
        s["completed_at"] = time.time()
        s["outcome"] = step_outcome or StepOutcome.SUCCESS
    # result 注入（去重 by result_hash）
    existing_hashes = {r.get("result_hash") for r in fresh_results}
    if result_hash not in existing_hashes:
        fresh_results.append({...})
    newly_ready = unlock_dependents(fresh_steps)
    return {
        "state": {**dict(fresh_flow["state"]), "steps": fresh_steps, "results": fresh_results},
        "status": "running" if _has_unfinished(fresh_steps) else "done",
        "newly_ready": newly_ready,
    }

updated, error = await update_flow_with_conflict_retry(
    store_sqlite, flow_id, revision, session_id,
    build_state=_build_done_state,
    push_session_id=session_id,
)
if error:
    return error  # conflict retry 耗尽
```

`update_flow_with_conflict_retry` 已在 `_shared.py:410-466` 实现（CAS + bounded retry，`PERSIST_MAX_ATTEMPTS = 3`），它的 `build_state` callback 接收 fresh flow + attempt number，返回要写的 state。关键是 callback 要能在**任意 fresh state** 上重新应用 step-done + result-injection + unlock 逻辑——而不是用第一次读的旧 steps。

#### 0b：`_auto_dispatch_ready` 改用 conflict retry + fresh steps

**文件**：`agent/tools/taskflow/tools/taskflow_resume.py:430-501`

```python
# 改前：第一次读 steps → spawn → 用旧 steps 写
# 改后：spawn → 用 update_flow_with_conflict_retry，callback 在 fresh steps 上应用 dispatched 标记

async def _build_dispatched_state(fresh_flow, attempt):
    fresh_steps = fresh_flow["state"]["steps"]
    by_id = {s["id"]: s for s in fresh_steps}
    for sid, child_key, dispatched_at in dispatched_records:
        if sid in by_id:
            s = by_id[sid]
            # 只更新尚未被其他 writer 改过的 step
            if s["status"] == StepStatus.READY:
                s["status"] = StepStatus.DISPATCHED
                s["child_session_key"] = child_key
                s["dispatched_at"] = dispatched_at
    return {
        "state": {**dict(fresh_flow["state"]), "steps": fresh_steps},
    }

updated, error = await update_flow_with_conflict_retry(
    store_sqlite, flow_id, revision, session_id,
    build_state=_build_dispatched_state,
    push_session_id=session_id,
)
```

这复用了 `apply_dispatched_steps`（`_shared.py:388-407`）的模式——匹配 step_id，在 fresh steps 上应用 dispatched payload，不存在的 step append（不丢失已 spawn child）。

#### 0c：evidence ledger 加文件锁 + 容错读

**文件**：`agent/tools/todolist/evidence_ledger.py`（或 `pub/func/message/evidence_ledger.py`，按实际路径）

```python
# 写路径加锁
from agent.tools.pub_base.file_lock import file_write_lock

class EvidenceLedger:
    @classmethod
    def append(cls, record: dict) -> None:
        with file_write_lock(cls.LEDGER_PATH):
            with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

# 读路径加 per-line 容错
@classmethod
def read_all(cls) -> list[dict]:
    with file_write_lock(cls.LEDGER_PATH):  # 共享读锁或改用 read_lock
        with open(cls.LEDGER_PATH, "r", encoding="utf-8") as f:
            records = []
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("evidence ledger partial line at {}: {}", i, line[:80])
                    continue
            return records
```

#### 0d：检查 resume 返回值

**文件**：`agent/tools/subagent/announce/delivery.py:366-407`

```python
# 改前:
answer = await taskflow_resume.coroutine(...)
logger.info("taskflow auto-resume answer: {}", answer)
return True  # 无条件

# 改后:
answer = await taskflow_resume.coroutine(...)
if isinstance(answer, str) and answer.startswith("Error:"):
    logger.warning("taskflow auto-resume failed: {}", answer)
    return False
logger.info("taskflow auto-resume answer: {}", answer)
return True
```

---

## 2. 并行化 spawn 循环（Phase 1）

Phase 0 修完后，三个 dispatch 循环可以从 `for + await` 改为 `asyncio.gather`。

### 2.1 批量 dispatch

**文件**：`agent/tools/taskflow/tools/taskflow_dispatch.py:116-144`

```python
# 改前:
for sid in requested:
    step = by_id[sid]
    task_text = ...
    try:
        child_key = await _dispatch.dispatch_child(
            task=task_text, requester_session_key=requester_key, label=None)
    except Exception as exc:
        failure = exc
        break
    step["status"] = StepStatus.DISPATCHED
    step["child_session_key"] = child_key
    step["dispatched_at"] = time.time()
    records.append(step)
    dispatched_ids.append(sid)

# 改后:
async def _spawn_one(sid: str) -> tuple[str, str | None, Exception | None]:
    step = by_id[sid]
    task_text = with_judge_feedback(
        build_task_with_bindings(
            build_task_with_dep_results(step, steps, results), step, steps, results),
        feedback)
    try:
        child_key = await _dispatch.dispatch_child(
            task=task_text, requester_session_key=requester_key, label=None)
        return sid, child_key, None
    except Exception as exc:
        return sid, None, exc

# 并发 spawn，带信号量限流
sem = asyncio.Semaphore(TASKFLOW_INFRA["max_parallel_dispatch"])
async def _limited_spawn(sid):
    async with sem:
        return await _spawn_one(sid)

results_batch = await asyncio.gather(*[_limited_spawn(sid) for sid in requested])

dispatched_records = []
failures = []
for sid, child_key, exc in results_batch:
    if exc is not None:
        failures.append((sid, exc))
        continue
    step = by_id[sid]
    step["status"] = StepStatus.DISPATCHED
    step["child_session_key"] = child_key
    step["dispatched_at"] = time.time()
    dispatched_records.append(step)

# 持久化成功的（一次 update_flow_with_conflict_retry）
updated, error = await update_flow_with_conflict_retry(
    store_sqlite, flow_id, revision, session_id,
    build_state=lambda fresh, attempt: _apply_dispatched(fresh, dispatched_records),
    push_session_id=session_id,
)
```

### 2.2 自动 dispatch

**文件**：`agent/tools/taskflow/tools/taskflow_resume.py:430-501` (`_auto_dispatch_ready`)

同 2.1 的模式：`newly_ready` 列表并发 spawn，收集成功的 dispatched records，一次 `update_flow_with_conflict_retry` 持久化。

### 2.3 重试 dispatch

**文件**：`agent/tools/taskflow/tools/taskflow_wait_all.py:143-169` (`_retry_settled_steps`)

同上模式。

---

## 3. 配置 + 背压（Phase 2）

### 3.1 新增配置

**文件**：`config/features/agent_side/taskflow_infra.py`

```python
class TaskflowInfraConfig(TypedDict):
    # ... 现有字段 ...
    max_parallel_dispatch: int  # 单次 dispatch 调用的最大并发 spawn 数

TASKFLOW_INFRA: TaskflowInfraConfig = {
    # ... 现有值 ...
    "max_parallel_dispatch": 2,  # 灰度起步，上限不超过 subagent_max_concurrent (8)
}
```

### 3.2 信号量背压

每个 dispatch 入口用 `asyncio.Semaphore(TASKFLOW_INFRA["max_parallel_dispatch"])` 限制并发 spawn 数。lane 系统已提供全局背压（SUBAGENT 8 slot，超限 PENDING 排队），per-dispatch 信号量防止单次 dispatch 炸出过多子 agent。

---

## 4. 已确认安全的组件（无需改动）

| 组件 | 文件 | 安全原因 |
|---|---|---|
| `StepJudge` | `agent/tools/taskflow/step_judge.py` | 无状态纯函数，per-step 隔离，不读/写共享可变状态。只接参数返回 `JudgeResult` |
| `CompletionJudge` | `agent/tools/subagent/spawn/completion_judge.py` | 无状态纯函数，per-run 隔离，fail-open |
| `push_taskflow_progress` | `agent/tools/taskflow/progress_push.py` | 只读同步 + best-effort WS push。并发安全（冗余帧无害） |
| SQLite store | `agent/tools/pub_base/sqlite_store.py` | WAL + busy_timeout 5s + CAS（`expected_revision` 乐观锁）。引擎层串行化写入 |
| `update_flow_with_conflict_retry` | `agent/tools/taskflow/tools/_shared.py:410-466` | 本身就是为并发设计的 CAS + bounded retry，Phase 0 只是让遗漏的路径也用它 |
| `dispatch_child` → `spawn_subagent_direct` | `agent/tools/taskflow/tools/_dispatch.py` | 返回后子 agent 是后台 asyncio task，lane 系统已管理并发 |
| `waves.py::compute_waves` | `agent/tools/taskflow/waves.py` | 纯 reporting，不改 dispatch |

---

## 5. 测试（Phase 3）

| 测试文件 | 场景 | 验证点 |
|---|---|---|
| `tests/agent/tools/taskflow/test_concurrent_resume.py` | 两个 step 同时完成 → 两个 `taskflow_resume` 并发 | 两个 result 都成功注入（conflict retry 验证） |
| `tests/agent/tools/taskflow/test_concurrent_dispatch.py` | 一个 step 完成后 unlock 3 个 ready → 并发 spawn | 3 个 child_session_key 都持久化 |
| `tests/agent/tools/taskflow/test_no_double_dispatch.py` | 同一 step 被两个并发 resume 触发 dispatch | 只 spawn 一次（CAS 验证） |
| `tests/agent/tools/taskflow/test_evidence_concurrent.py` | 并发写 evidence ledger | 无 partial line 崩溃 |
| `tests/agent/tools/taskflow/test_parallel_dispatch_failure.py` | 5 个 step 并发 dispatch，2 个失败 | 3 个成功的持久化 + 2 个失败的报告 |

测试技巧：用 `asyncio.gather` 模拟并发 resume，monkeypatch `_dispatch.dispatch_child` 返回可控 child key，验证 SQLite 最终状态。

---

## 6. 实施顺序

```
Phase 0 (硬前置，无新功能):
  ├─ 0a. taskflow_resume 正常路径加 conflict retry
  ├─ 0b. _auto_dispatch_ready 改用 conflict retry + fresh steps
  ├─ 0c. evidence ledger 加 file_write_lock + per-line try/except
  └─ 0d. _try_auto_resume_taskflow 检查返回值

Phase 1 (并行化):
  ├─ 1a. taskflow_dispatch 改 asyncio.gather
  ├─ 1b. _auto_dispatch_ready 改 asyncio.gather
  └─ 1c. _retry_settled_steps 改 asyncio.gather

Phase 2 (配置):
  ├─ 2a. TASKFLOW_INFRA 加 max_parallel_dispatch
  └─ 2b. dispatch 入口加 Semaphore 背压

Phase 3 (血缘指针):
  ├─ 3a. task_flows 加 resumed_from + superseded_by 列（additive migration）
  ├─ 3b. taskflow_replan 写入两列
  └─ 3c. 测试：replan 后血缘链可追溯

Phase 4 (测试):
  └─ 5 个并发场景测试 + 血缘指针测试
```

---

## 7. 风险

| 风险 | 对策 |
|---|---|
| `build_state` callback 正确性 | callback 必须能在任意 fresh state 上重新应用 step-done + result-injection + unlock。`apply_dispatched_steps` 已为 redispatch 路径做了同构操作，模式相同 |
| 并发 resume 的 idempotency check | `result_hash` 去重已存在（`taskflow_resume:137-144`），并发交付同一 result 不会重复注入 |
| 信号量死锁 | `dispatch_child` 内部不获取 flow 级锁（只过 lane slot），信号量不会与 CAS retry 嵌套 |
| `max_parallel_dispatch` 设太大 | 默认 2 灰度，上限不超过 `subagent_max_concurrent`（8）。lane 系统已有全局背压 |
| 向后兼容 | `max_parallel_dispatch = 1` 时行为与改前完全一致（`asyncio.gather` 单元素 = 顺序执行） |

---

## 8. 血缘指针：`resumed_from` + `superseded_by`

> 从 `计划_workflow_journal重放.md` 合并。journal 重放计划中的 ① `input_hash`、② `PAUSED`、④ 步骤级缓存经分析均不需要（DAG 结构已防止 DONE step 被重跑、PAUSED 不杀子 agent 无实际效果、result 已在 `state["results"]` 被复用），仅保留血缘指针。

### 8.1 设计

`task_flows` 表加两列：

```sql
ALTER TABLE task_flows ADD COLUMN resumed_from TEXT;
ALTER TABLE task_flows ADD COLUMN superseded_by TEXT;
```

additive migration（`store_sqlite.py` 的 `_ADDITIVE_COLUMN_DDLS` 模式，旧 DB 加列、新 DB 在 CREATE 里带上）。

### 8.2 语义

| 列 | 写入时机 | 含义 |
|---|---|---|
| `resumed_from` | flow 创建时（从另一个 flow 修订来的） | 前驱 flow_id |
| `superseded_by` | 新 flow 取代旧 flow 时 | 后继 flow_id |

### 8.3 写入点

- `taskflow_replan`：生成新 flow 时，旧 flow 标 `superseded_by = new_flow_id`，新 flow 标 `resumed_from = old_flow_id`
- `taskflow_create`：无前驱时两列均为 NULL
- `taskflow_finish`：finish 不改这两列（finish 是终态，不是取代）

### 8.4 UI 能力

进度面板可以沿 `resumed_from` 链追溯 flow 演进历史，沿 `superseded_by` 知道"这个旧 run 被谁取代了"。`get_active_flows_sync` 不返回 superseded 的 flow。

### 8.5 改动

| 文件 | 改动 |
|---|---|
| `agent/tools/taskflow/registry/store_sqlite.py` | `task_flows` 加 `resumed_from` + `superseded_by` 列；additive migration |
| `agent/tools/taskflow/tools/taskflow_replan.py` | 写入两列 |

### 8.6 测试

| 测试文件 | 场景 | 验证点 |
|---|---|---|
| `tests/agent/tools/taskflow/test_lineage.py` | replan 生成新 flow | 旧 flow `superseded_by` = 新 id，新 flow `resumed_from` = 旧 id |

### 8.7 风险

| 风险 | 对策 |
|---|---|
| 血缘链断裂（replan 时旧 flow 已 finish） | `superseded_by` 只在 replan 写入；finish 不改两列；superseded 的 flow 不是 active 的 |
