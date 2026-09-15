# 全局 Lane 系统 — 完整实现计划

> **状态**：已完成（2026-09-15）
> **完成记录**：Wave 1–3 已全部落地并通过验证 —— `LANE_SYSTEM` + `runtime/lane/` 核心（含 23 个单测）、PENDING + SUBAGENT lane（kill/steer/sweeper/orphan/yield 全覆盖）、NUDGE/MAIN/NESTED lane + `server/service/lane_lifecycle.py` 装配 + `GET /lane-status` + 有界退出 drain；三处偏离已如实写入文档：MAIN 默认钳制到 `≥ SUBAGENT + NUDGE`、端点为 `server/trigger/http/lane.py` 的 `GET /lane-status`（按仓库路由惯例，无 `/api` 前缀）、退出 drain 走 `atexit`（仓库无异步关闭路径，`drain_all(timeout=0)` 不阻塞退出）；文档见 `AGENTS.md` 的「Concurrency Lanes」与 `docs/long-running-tasks/README{,.zh,.ja,.ko}.md` 的「并发 Lane / 並行レーン / 동시성 레인」。

> 对标 OpenClaw 的 `CommandLane` 多 lane 并发排队架构，将 Sherry 的 agent 并发控制从"计数器拒绝"升级为"全局多 lane 排队"。

---

## 目录

1. [现状分析](#1-现状分析)
2. [Lane 定义](#2-lane-定义)
3. [新文件: 配置 + 核心](#3-新文件-配置--核心)
4. [PENDING 状态](#4-pending-状态)
5. [Subagent Lane 集成](#5-subagent-lane-集成)
6. [Registry 计数函数更新](#6-registry-计数函数更新)
7. [Kill/Cancel 对 PENDING 的处理](#7-killcancel-对-pending-的处理)
8. [Sweeper/Orphan Recovery 更新](#8-sweeperorphan-recovery-更新)
9. [run_async() Event Loop 兼容性](#9-run_async-event-loop-兼容性)
10. [Swarm Collector 交互](#10-swarm-collector-交互)
11. [Main Agent Lane 集成](#11-main-agent-lane-集成)
12. [Nested Lane 集成](#12-nested-lane-集成)
13. [Sessions Yield 交互](#13-sessions-yield-交互)
14. [Server 启动初始化](#14-server-启动初始化)
15. [Work Admission / Graceful Shutdown](#15-work-admission--graceful-shutdown)
16. [Lane 状态 API](#16-lane-状态-api)
17. [Config 校验](#17-config-校验)
18. [死锁防护](#18-死锁防护)
19. [向后兼容](#19-向后兼容)
20. [文件变更清单](#20-文件变更清单)
21. [测试计划](#21-测试计划)

---

## 1. 现状分析

| 工作类型           | 当前控制方式                                                                   | 问题                        |
| ------------------ | ------------------------------------------------------------------------------ | --------------------------- |
| Main agent turn    | per-session 串行（`UserInputQueue` + `SessionState.detect_state`），无全局上限 | 多 session 并发时无全局保护 |
| Subagent 执行      | `validate_global_concurrent()` 计数器拒绝（`max_concurrent=8`）                | 满了直接 forbidden，不排队  |
| Nudge agent        | 无限制（`run_async()` fire-and-forget + `asyncio.gather()`）                   | 可能耗尽 LLM 连接池         |
| sessions.send 回复 | 无限制                                                                         | —                           |

### 关键代码路径

- **Subagent spawn**: `spawn/core.py` Phase 2 (`validate_global_concurrent`) → Phase 6 (`register_run` with `ExecutionStatus.RUNNING`) → Phase 11 (`asyncio.create_task(_execute_subagent(...))`)
- **Main turn dispatch**: `input_queue_service.py:385` `asyncio.create_task(_run_executor(...))`
- **Nudge (sync path)**: `context_engine/core.py:301` `run_async(_nudge_memory(...))` — 独立线程 + 独立 event loop
- **Nudge (async path)**: `context_engine/core.py:348` `await asyncio.gather(_persist(), _nudge())` — 主 event loop
- **Kill**: `control/kill.py:61` `cancel_task(run_id)` → `_clear_session_queues()` → `complete_subagent_run()`
- **Steer**: `control/steer.py:78` `cancel_task(run_id)` → `_abort_settle_wait()` → `replace_run_after_steer()` → `_execute_steered_subagent()`
- **Sweeper**: `registry/sweeper.py:58` `_do_sweep()` → `recover_orphaned_runs()` + `scan_orphaned_sessions()` + `_finalize_killed_unterminated()`
- **Orphan recovery**: `orphan/recovery.py:94` `evaluate_recovery_gate()` — 检查 `started_at` for wedged detection
- **Registry counting**: `queries.py:29-42` `count_active_runs_for_session()` / `count_all_active_runs()` / `count_active_descendant_runs()` — 只计 RUNNING
- **Work admission**: `work_admission.py` — `set_draining(True)` + `run_with_work_admission()` — graceful shutdown 门控

---

## 2. Lane 定义

| Lane       | 对标 OpenClaw          | 约束对象                | 默认并发                                | 来源                                              |
| ---------- | ---------------------- | ----------------------- | --------------------------------------- | ------------------------------------------------- |
| `MAIN`     | `CommandLane.Main`     | 主 agent turn           | `min(16, max(8, CPU))` — CPU 缩放，8~16 | `agent-limits.ts:resolveAgentMaxConcurrent`       |
| `SUBAGENT` | `CommandLane.Subagent` | 子 agent 执行体         | `8`                                     | `agent-limits.ts:DEFAULT_SUBAGENT_MAX_CONCURRENT` |
| `NUDGE`    | —（OpenClaw 无对应）   | nudge agent 调用        | `4`                                     | 新增                                              |
| `NESTED`   | `CommandLane.Nested`   | sessions.send 回复 turn | `1`（串行）                             | `server-lanes.ts:98`                              |

> OpenClaw 还有 `CRON`、`CRON_NESTED`、`HOOK_DISPATCH`、`SYSTEM_AGENT` 等 lane。Sherry 的 cron/heartbeat 走 main agent turn，不单独设 lane。如后续拆分 cron 独立调度，再追加 `CRON` lane。

---

## 3. 新文件: 配置 + 核心

### 3.1 配置: `config/features/infra_side/lane_system.py`

```python
"""Global lane concurrency limits for agent work dispatch."""

import os
from typing import TypedDict


class LaneSystemConfig(TypedDict):
    """Per-lane concurrency limits. 0 = unlimited."""

    main_max_concurrent: int
    subagent_max_concurrent: int
    nudge_max_concurrent: int
    nested_max_concurrent: int
    lane_wait_warn_ms: int
    lane_drain_timeout_seconds: float


def _resolve_main_concurrency() -> int:
    """CPU-scaled, bounded 8~16 — mirrors OpenClaw resolveAgentMaxConcurrent."""
    cpu = os.cpu_count() or 8
    return min(16, max(8, cpu))


LANE_SYSTEM: LaneSystemConfig = {
    "main_max_concurrent": _resolve_main_concurrency(),
    "subagent_max_concurrent": 8,
    "nudge_max_concurrent": 4,
    "nested_max_concurrent": 1,
    "lane_wait_warn_ms": 5000,
    "lane_drain_timeout_seconds": 30.0,
}
```

在 `config/features/infra_side/__init__.py` 中 re-export `LANE_SYSTEM` / `LaneSystemConfig`。

> **import-linter 约束**: `config/features/**` MUST NOT import from `agent/`、`server/`、`models/`。此文件只依赖 `os` 和 `typing`，合规。

### 3.2 核心: `runtime/lane/`

新建 `runtime/lane/` 包（与 `runtime/session/`、`runtime/process/` 平级）。

#### `runtime/lane/core.py`

```python
"""Process-level multi-lane concurrency queue with FIFO ordering."""

import asyncio
import time
from contextlib import asynccontextmanager
from enum import StrEnum
from loguru import logger


class LaneType(StrEnum):
    MAIN = "main"
    SUBAGENT = "subagent"
    NUDGE = "nudge"
    NESTED = "nested"


class Lane:
    """Single concurrency lane: asyncio.Semaphore + counters.

    The Semaphore is lazily created on first acquire() to ensure it binds
    to the running event loop. If the loop changes (shouldn't happen in
    production — single main loop), call _rebind().
    """

    def __init__(self, name: str, max_concurrent: int):
        self.name = name
        self._max = max_concurrent
        self._sem: asyncio.Semaphore | None = None
        self._active = 0
        self._queued = 0
        self._waiters: list[asyncio.Future] = []  # for drain support

    def _ensure_sem(self):
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max)

    async def acquire(self, warn_ms: int = 5000) -> None:
        self._ensure_sem()
        self._queued += 1
        wait_start = time.monotonic()
        await self._sem.acquire()
        self._queued -= 1
        waited_ms = (time.monotonic() - wait_start) * 1000
        if waited_ms > warn_ms:
            logger.warning(
                "Lane {} waited {:.0f}ms for slot (active={}, queued={})",
                self.name, waited_ms, self._active, self._queued,
            )
        self._active += 1

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()
        self._active -= 1

    async def drain(self, timeout: float = 30.0) -> bool:
        """Wait for all active slots to release. Returns True if drained in time."""
        deadline = time.monotonic() + timeout
        while self._active > 0:
            if time.monotonic() >= deadline:
                logger.warning(
                    "Lane {} drain timed out: {} active slots remain",
                    self.name, self._active,
                )
                return False
            await asyncio.sleep(0.1)
        return True

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def queued_count(self) -> int:
        return self._queued

    @property
    def max_concurrent(self) -> int:
        return self._max

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "max_concurrent": self._max,
            "active": self._active,
            "queued": self._queued,
        }


class LaneManager:
    """Process-level singleton managing all lanes."""

    def __init__(self):
        from config.features import LANE_SYSTEM
        self._lanes: dict[LaneType, Lane] = {
            LaneType.MAIN: Lane("main", LANE_SYSTEM["main_max_concurrent"]),
            LaneType.SUBAGENT: Lane("subagent", LANE_SYSTEM["subagent_max_concurrent"]),
            LaneType.NUDGE: Lane("nudge", LANE_SYSTEM["nudge_max_concurrent"]),
            LaneType.NESTED: Lane("nested", LANE_SYSTEM["nested_max_concurrent"]),
        }
        self._warn_ms = LANE_SYSTEM["lane_wait_warn_ms"]
        self._drain_timeout = LANE_SYSTEM["lane_drain_timeout_seconds"]

    def get_lane(self, lane_type: LaneType) -> Lane:
        return self._lanes[lane_type]

    def set_concurrency(self, lane_type: LaneType, max_concurrent: int) -> None:
        """Hot-update: creates a new Semaphore; in-flight slots unaffected."""
        lane = self._lanes[lane_type]
        lane._max = max_concurrent
        lane._sem = asyncio.Semaphore(max_concurrent)

    async def drain_all(self, timeout: float | None = None) -> dict[str, bool]:
        """Drain all lanes for graceful shutdown."""
        t = timeout or self._drain_timeout
        results = {}
        for lt, lane in self._lanes.items():
            results[lt.value] = await lane.drain(t)
        return results

    def snapshot(self) -> dict[str, dict]:
        return {lt.value: lane.snapshot() for lt, lane in self._lanes.items()}


_manager: LaneManager | None = None


def get_lane_manager() -> LaneManager:
    global _manager
    if _manager is None:
        _manager = LaneManager()
    return _manager


@asynccontextmanager
async def lane_slot(lane_type: LaneType):
    """Acquire a lane slot; waits in queue when full. Releases on exit/exception."""
    lane = get_lane_manager().get_lane(lane_type)
    await lane.acquire(warn_ms=get_lane_manager()._warn_ms)
    try:
        yield
    finally:
        lane.release()
```

#### `runtime/lane/__init__.py`

```python
"""Process-level multi-lane concurrency queue."""

from .core import Lane, LaneManager, LaneType, get_lane_manager, lane_slot

__all__ = ["Lane", "LaneManager", "LaneType", "get_lane_manager", "lane_slot"]
```

---

## 4. PENDING 状态

### 文件: `agent/tools/subagent/types/registry.py`

```python
class ExecutionStatus(StrEnum):
    PENDING = "pending"      # 新增: 已注册，等待 lane slot
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    TERMINAL = "terminal"
```

### 文件: `agent/tools/subagent/registry/run_manager.py:90-92`

`register_run` 中 `ExecutionState` 的初始 status 改为 `PENDING`：

```python
execution=ExecutionState(
    status=ExecutionStatus.PENDING,    # 改: 原 RUNNING
    started_at=None,                     # 改: 原 time.monotonic() — PENDING 时未开始
),
```

> **注意**: `started_at=None` 意味着 `is_stale_unended_run()` 和 `reconcile_orphaned_run()` 都会跳过 PENDING run（它们检查 `started_at is None` → 返回 False/None）。这正是我们想要的行为——PENDING run 不应被判为 stale。

---

## 5. Subagent Lane 集成

### 5.1 文件: `agent/tools/subagent/spawn/core.py`

#### Phase 2 — Admission gate（约 line 251-254）

- **删除** `validate_global_concurrent()` 调用 — lane 接管全局并发控制
- **保留** `validate_spawn_depth()` + `validate_concurrent_children()` — per-parent admission gate（与 OpenClaw 一致：admission gate + lane 双层）

#### Phase 6 — Registration

`register_run` 已在 Step 4 改为 PENDING，无需额外修改。

#### Phase 11 — Background task launch（约 line 452-463）

将 `asyncio.create_task(_execute_subagent(...))` 替换为 lane-wrapped 版本：

```python
async def _execute_subagent_with_lane(run, **kwargs):
    """Lane-wrapped subagent execution: PENDING → RUNNING transition inside lane slot."""
    from runtime.lane import lane_slot, LaneType
    from ..registry.memory import update as update_run
    from ..registry.run_manager import mark_run_running

    async with lane_slot(LaneType.SUBAGENT):
        # PENDING → RUNNING transition (only if not already killed/steered)
        run = get_run(run.run_id)
        if run is None or run.execution.status == ExecutionStatus.TERMINAL:
            return  # killed while waiting for lane slot
        if run.execution.status == ExecutionStatus.PENDING:
            updated = mark_run_running(run.run_id)
            if updated is None:
                return  # race: run disappeared
            run = updated
        await _execute_subagent(run, **kwargs)

bg_task = asyncio.create_task(_execute_subagent_with_lane(run, ...))
```

> **关键**: `mark_run_running()` 目前只处理 INTERRUPTED→RUNNING。需扩展为同时支持 PENDING→RUNNING（设置 `started_at=time.monotonic()`）。

### 5.2 文件: `agent/tools/subagent/registry/run_manager.py`

扩展 `mark_run_running()` 以支持 PENDING→RUNNING：

```python
def mark_run_running(run_id: str) -> SubagentRunRecord | None:
    """Transition a PENDING or INTERRUPTED run to RUNNING."""
    run = memory.get(run_id)
    if run is None:
        return None

    if run.execution.status not in (ExecutionStatus.PENDING, ExecutionStatus.INTERRUPTED):
        return run  # already running or terminal — no-op

    updated = run.model_copy(
        update={
            "execution": run.execution.model_copy(
                update={
                    "status": ExecutionStatus.RUNNING,
                    "started_at": time.monotonic(),  # set started_at for PENDING runs
                }
            ),
            "pause_reason": None,
        }
    )
    memory.set_run(updated)
    return updated
```

### 5.3 文件: `agent/tools/subagent/spawn/depth.py`

`validate_global_concurrent()` 标记 deprecated：

```python
def validate_global_concurrent(config: SubagentConfig) -> None:
    """[DEPRECATED] Global concurrency is now controlled by the SUBAGENT lane.

    This function is retained for backward compatibility but no longer called
    by spawn/core.py. The lane system provides queueing instead of rejection.
    """
    import warnings
    warnings.warn(
        "validate_global_concurrent is deprecated; use LaneType.SUBAGENT instead",
        DeprecationWarning,
        stacklevel=2,
    )
    # 旧逻辑保留但不执行
```

---

## 6. Registry 计数函数更新

### 6.1 文件: `agent/tools/subagent/registry/queries.py`

PENDING run 已注册但未执行。各计数函数需区分语义：

| 函数                              | 当前         | 改后                 | 理由                                                        |
| --------------------------------- | ------------ | -------------------- | ----------------------------------------------------------- |
| `count_active_runs_for_session()` | 只计 RUNNING | 计 RUNNING + PENDING | per-parent children budget: PENDING child 占一个 spawn slot |
| `count_all_active_runs()`         | 只计 RUNNING | 计 RUNNING + PENDING | 全局状态视图                                                |
| `count_active_descendant_runs()`  | 只计 RUNNING | 计 RUNNING + PENDING | descendant 活跃度检查                                       |

```python
def count_active_runs_for_session(session_key: str) -> int:
    """Count runs with RUNNING or PENDING status for the given requester session."""
    return sum(
        1
        for run in memory.values()
        if run.requester_session_key == session_key
        and run.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.PENDING)
    )


def count_active_descendant_runs(session_key: str) -> int:
    """Count RUNNING or PENDING runs among all descendants of the given session."""
    descendants = list_descendant_runs(session_key)
    return sum(
        1
        for run in descendants
        if run.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.PENDING)
    )


def count_all_active_runs() -> int:
    """Count all RUNNING or PENDING runs across all sessions (global concurrency view)."""
    return sum(
        1
        for run in memory.values()
        if run.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.PENDING)
    )
```

### 6.2 文件: `agent/tools/subagent/registry/read.py`

`count_active_runs_readonly` / `count_active_descendant_runs_readonly` / `count_all_active_runs_readonly` 转发到 queries.py，自动继承新语义。

### 6.3 文件: `agent/tools/subagent/registry/helpers.py`

`is_live_unended_run()` 需包含 PENDING：

```python
def is_live_unended_run(run: SubagentRunRecord) -> bool:
    """Return True if the run is PENDING, RUNNING, or INTERRUPTED (not yet terminated)."""
    return run.execution.status in (
        ExecutionStatus.PENDING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.INTERRUPTED,
    )
```

> **影响**: `should_keep_child_link()` 也会包含 PENDING — 正确，一个 PENDING child 的 link 应该保留。

---

## 7. Kill/Cancel 对 PENDING 的处理

### 7.1 文件: `agent/tools/subagent/control/kill.py`

#### `resolve_kill_target_state()`

```python
def resolve_kill_target_state(run: SubagentRunRecord) -> str:
    """Determine if a run is 'killable', 'finalizing', or already 'terminal'."""
    if run.execution.status == ExecutionStatus.TERMINAL:
        return "terminal"
    if run.kill_reconciliation is not None and not run.kill_reconciliation.reconciled:
        return "finalizing"
    return "killable"  # PENDING, RUNNING, INTERRUPTED 都是 killable
```

> 无需修改 — PENDING 自然落入 "killable" 分支。

#### `kill_subagent_run()` 关键路径

现有逻辑：

1. `save_kill_reconciliation(run_id)` — 快照当前 execution state
2. `cancel_task(run_id)` — 取消 asyncio.Task
3. `_clear_session_queues(child_session_key)` — 清理 session 队列
4. `complete_subagent_run(run_id, KILLED)` — 标记 TERMINAL

**PENDING run 被杀时**：`cancel_task(run_id)` 会 cancel `_execute_subagent_with_lane` 这个 asyncio.Task。此时 task 正在 `await lane.acquire()` 等待 lane slot。`CancelledError` 从 `lane.acquire()` 传播到 `_execute_subagent_with_lane` 的 `async with lane_slot(...)` 上下文管理器。`lane_slot` 的 `finally: lane.release()` 会被调用——但等等，`release()` 会做 `self._sem.release()`，而此时 acquire 尚未完成（Semaphore 未被 acquire），这会导致 Semaphore 计数错误。

**修复**: `Lane.acquire()` 需处理取消：

```python
async def acquire(self, warn_ms: int = 5000) -> None:
    self._ensure_sem()
    self._queued += 1
    wait_start = time.monotonic()
    try:
        await self._sem.acquire()
    except asyncio.CancelledError:
        self._queued -= 1
        raise  # 不释放 sem — acquire 未成功
    self._queued -= 1
    waited_ms = (time.monotonic() - wait_start) * 1000
    if waited_ms > warn_ms:
        logger.warning(...)
    self._active += 1
```

> **关键**: `asyncio.Semaphore.acquire()` 被取消时不会消耗 permit，所以不需要 release。但 `_queued` 计数必须修正。`lane_slot` 的 `finally: lane.release()` 只在 acquire 成功后才执行——等等，`asynccontextmanager` 的 `finally` 在 `yield` 之后执行，如果 `acquire()` 本身抛出 `CancelledError`，`yield` 不会被执行，`finally` 也不会执行。所以实际上是安全的。

**但需要验证**: `@asynccontextmanager` 的行为——如果 `__aenter__`（即 `acquire()`）抛出异常，`__aexit__` 不会被调用。确认无误。

#### `_clear_session_queues()` 对 PENDING

```python
async def _clear_session_queues(child_session_key: str) -> None:
    task = get_task(child_session_key)
    if task and not task.done():
        task.cancel()
```

对 PENDING run，`get_task(run_id)` 返回的是 `_execute_subagent_with_lane` task。`cancel()` 会触发上述 `CancelledError` 路径。正确。

#### `list_killable_children()`

```python
def list_killable_children(session_key: str) -> list[SubagentRunRecord]:
    children = list_runs_for_requester(session_key)
    return [
        c
        for c in children
        if c.execution.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)
    ]
```

### 7.2 文件: `agent/tools/subagent/control/steer.py`

#### `steer_subagent_run()` 对 PENDING run

当前检查 `run.execution.status not in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)` 时返回 warning。PENDING run 不能被 steer（它还没开始执行，没有可 steer 的上下文）。

**修改**: 加入 PENDING 到不可 steer 列表：

```python
if run.execution.status not in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED):
    logger.warning(
        "steer_subagent_run: run {} not steerable (status={})", run_id, run.execution.status
    )
    return None
```

> 无需修改 — PENDING 不在 `(RUNNING, INTERRUPTED)` 中，所以会被拒绝。正确行为。

#### `_execute_steered_subagent()` 也需 lane wrapping

steer 重启的 subagent 也应经过 SUBAGENT lane：

```python
async def _execute_steered_subagent(run, child_agent, steer_message, timeout_seconds):
    from runtime.lane import lane_slot, LaneType

    async with lane_slot(LaneType.SUBAGENT):
        # ... existing _execute_steered_subagent body ...
```

> **注意**: steer 的 run 在 `replace_run_after_steer` 后 status 是 RUNNING（line 153-160）。但如果 lane 满了，它应该排队。所以 steer 重启的 run 也应先回退到 PENDING，再走 lane。

**修改 steer 重启路径**:

```python
# steer.py:149-161 — 替换为:
restarted = updated.model_copy(
    update={
        "execution": updated.execution.model_copy(
            update={
                "status": ExecutionStatus.PENDING,  # 改: 先 PENDING，lane 内转 RUNNING
                "started_at": None,                  # 改: 清空，lane 内设置
            }
        ),
        "pause_reason": None,
        "suppress_announce_reason": None,
    }
)
set_run(restarted)

# _execute_steered_subagent 改为 _execute_steered_subagent_with_lane:
async def _execute_steered_subagent_with_lane(run, child_agent, steer_message, timeout_seconds):
    from runtime.lane import lane_slot, LaneType
    from ..registry.run_manager import mark_run_running

    async with lane_slot(LaneType.SUBAGENT):
        current = get_run(run.run_id)
        if current is None or current.execution.status == ExecutionStatus.TERMINAL:
            return  # killed while waiting
        mark_run_running(run.run_id)
        # ... existing _execute_steered_subagent try/except/finally body ...
```

---

## 8. Sweeper/Orphan Recovery 更新

### 8.1 文件: `agent/tools/subagent/orphan/recovery.py`

#### `evaluate_recovery_gate()`

```python
def evaluate_recovery_gate(run: SubagentRunRecord) -> str:
    """Classify a run as 'wedged', 'aborted_last_run', or 'recoverable'."""
    if run.execution.started_at is not None:
        age = time.monotonic() - run.execution.started_at
        if age > _WEDGED_AGE_SECONDS:
            return "wedged"
    # PENDING run: started_at is None → skip wedged check
    # PENDING run is "recoverable" — it should be resumed/steered
    ...
```

> 无需修改 — `started_at is None` 时跳过 wedged 检查，落入 "recoverable" 或 "aborted_last_run"。PENDING run 的 `aborted_last_run` 默认 False，所以返回 "recoverable"。正确。

#### `scan_orphaned_sessions()`

```python
async def scan_orphaned_sessions() -> list[SubagentRunRecord]:
    from ..registry import get_task

    orphans = []
    for run in all_runs():
        if not is_live_unended_run(run):
            continue  # PENDING is now included in is_live_unended_run (Step 6.3)
        ...
        task = get_task(run.run_id)
        if task is None or task.done():
            orphans.append(run)
```

> PENDING run 被 `is_live_unended_run()` 包含后，会被 sweeper 扫描。如果其 task 仍在 `lane.acquire()` 中等待，`task.done()` 是 False，不会被判定为 orphan。如果进程崩溃重启，task 是 None，会被判定为 orphan → `schedule_orphan_recovery()` → `_attempt_resume()` → `steer_subagent_run()`。

> **问题**: `steer_subagent_run()` 拒绝 PENDING run（Step 7.2）。所以 `_attempt_resume()` 对 PENDING run 会失败，落入 `finalize_interrupted_run_with_retry()` → 标记为 TERMINAL/TIMEOUT。

> **修复**: `evaluate_recovery_gate()` 需对 PENDING run 特殊处理——直接 finalize 而非 resume：

```python
def evaluate_recovery_gate(run: SubagentRunRecord) -> str:
    if run.execution.started_at is not None:
        age = time.monotonic() - run.execution.started_at
        if age > _WEDGED_AGE_SECONDS:
            return "wedged"
    # PENDING run that lost its task (process restart) — cannot resume,
    # finalize as TIMEOUT
    if run.execution.status == ExecutionStatus.PENDING:
        return "wedged"  # treat as unrecoverable
    ...
```

### 8.2 文件: `agent/tools/subagent/registry/sweeper.py`

#### `_finalize_killed_unterminated()`

当前只检查 `TERMINAL + ended_reason == "killed"`。不受 PENDING 影响。

#### sweeper 中的 TaskFlow stale WAITING 扫描

`_scan_stale_waiting_taskflows()` 调用 `is_live_unended_run(run)` 检查 child 是否活跃。PENDING run 现在会被视为 live，所以 stale WAITING TaskFlow 的 child 如果在 PENDING 状态不会被标记为 stale。正确——PENDING 意味着 child 即将执行。

### 8.3 文件: `agent/tools/subagent/registry/helpers.py`

#### `reconcile_orphaned_run()`

```python
def reconcile_orphaned_run(run: SubagentRunRecord) -> SubagentRunRecord | None:
    if not is_live_unended_run(run):
        return None
    if run.execution.started_at is None:
        return None  # PENDING: no started_at, skip (correct)
    ...
```

> 无需修改 — PENDING 的 `started_at is None` 会被跳过。但结合 Step 8.1 的修复（PENDING → "wedged"），`_recovery_loop` 会对 PENDING run 调用 `reconcile_orphaned_run()`，后者返回 None。然后 `_recovery_loop` 会 `set_run(updated)` 但 updated 是 None——**bug**。

> **修复**: `_recovery_loop` 需处理 PENDING run 的特殊 finalize：

```python
# recovery.py:_recovery_loop, gate_result == "wedged" 分支:
if gate_result == "wedged":
    if run.execution.status == ExecutionStatus.PENDING:
        # PENDING run lost its task — finalize directly as TIMEOUT
        from ..types.registry import ExecutionState
        updated = run.model_copy(
            update={
                "execution": ExecutionState(
                    status=ExecutionStatus.TERMINAL,
                    started_at=None,
                    ended_at=time.monotonic(),
                    outcome=RunOutcome(status=RunOutcomeStatus.TIMEOUT, error="pending orphaned"),
                ),
                "ended_reason": "pending_orphaned",
            }
        )
        set_run(updated)
        await run_subagent_announce_flow(updated)
    else:
        updated = reconcile_orphaned_run(run)
        if updated is not None:
            set_run(updated)
            await run_subagent_announce_flow(updated)
    _recovery_tasks.pop(run_id, None)
    return
```

---

## 9. run_async() Event Loop 兼容性

### 问题

`run_async()` (`pub/func/run_async.py`) 在 ThreadPoolExecutor 中创建独立 event loop。`asyncio.Semaphore` 是 event-loop-bound 对象——在 loop A 中创建的 Semaphore 不能在 loop B 中 await。

Nudge agent 有两条路径：

1. **Sync path** (`context_engine/core.py:301`): `run_async(_nudge_memory(...))` — 在新线程/新 loop 中执行
2. **Async path** (`context_engine/core.py:348`): `await asyncio.gather(_persist(), _nudge())` — 主 loop

如果 `_nudge_memory` 内部 `async with lane_slot(LaneType.NUDGE):` 使用 `asyncio.Semaphore`，该 Semaphore 在 `LaneManager.__init__` 时（主 loop）创建——不对，`Lane._ensure_sem()` 是 lazy 的，在第一次 `acquire()` 时创建。如果第一次 acquire 来自 `run_async()` 的新 loop，Semaphore 就绑定到那个 loop。下次从主 loop acquire 就会报错。

### 方案: 重构 sync path 为 asyncio.create_task

将 `context_engine/core.py` 的 `after_agent`（sync 方法）中的 `run_async()` 替换为不经过 `run_async` 的方式。

**分析 `after_agent` 调用链**:

- `after_agent` 是 sync 方法（`def after_agent`，不是 `async def`）
- 它被 LangChain middleware pipeline 在 `after_agent` 阶段调用
- `aafter_agent` 是 async 版本，做完全相同的事 + 额外的 message persistence

**方案**: 删除 `after_agent` 中的 nudge 调用，只保留 `aafter_agent` 中的 nudge 调用。如果 sync `after_agent` 被调用，说明不在 async context——但 LangChain 1.3 的 middleware pipeline 总是调用 async 版本。

**验证**: 搜索 `after_agent` 的调用方。LangChain 1.3.9 中，如果 `aafter_agent` 存在，`after_agent` 不会被调用（async 优先）。所以 `after_agent` 中的 `run_async` 路径是 dead code。

**修改** (`context_engine/core.py:291-305`):

```python
@override
def after_agent(self, state: StateT, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
    # Nudge is now only dispatched from aafter_agent (async path).
    # The sync after_agent is retained for middleware protocol compliance
    # but does not fire nudge — run_async() is incompatible with the
    # event-loop-bound NUDGE lane semaphore.
    result = self._after_agent_impl(state)
    if result is None:
        return None
    # Persist only — no nudge from sync path
    return None
```

> **风险**: 如果有调用方确实走了 sync `after_agent` 路径，nudge 不会被触发。但 `aafter_agent` 是 async override，LangChain 优先调用它。

### 方案: delegate_task 的 run_async 路径

`delegate_task` 也通过 `run_async()` 调用——它创建新 loop。如果 `delegate_task` 内部 spawn 了 subagent，subagent 的 `lane_slot(LaneType.SUBAGENT)` 会在 delegate_task 的 loop 中创建 Semaphore。

**但实际上**: `delegate_task` 调用 `spawn_subagent_direct()`，后者最终调用 `asyncio.create_task(_execute_subagent_with_lane(...))` — 这要求一个运行中的 event loop。`run_async` 提供了这个 loop（`worker_loop.run_until_complete`）。所以 `asyncio.create_task` 会在 worker_loop 上调度 task。`lane_slot` 的 Semaphore 也会在 worker_loop 上创建——但之后主 loop 上的 subagent spawn 会尝试使用同一个 Semaphore，跨 loop 报错。

**修复**: `delegate_task` 应改为不使用 `run_async`。它应从调用方的 event loop 中 `await` spawn。但 `delegate_task` 被 LangChain tool 调用，tool handler 可以是 async——`_arun` 方法。

**验证**: 检查 `delegate_task` 是否有 sync `_run` 和 async `_arun`。如果有 `_arun`，LangChain 会优先调用 async 版本，`run_async` 路径是 fallback。

> **此步骤需要进一步调查 `delegate.py` 的实际调用路径。** 如果 `_arun` 存在且被优先调用，`run_async` 路径可安全废弃。

---

## 10. Swarm Collector 交互

### 文件: `agent/tools/subagent/swarm/collector.py`

Swarm 的 `_pump_lane()` (line 210) 控制 per-group 并发（`config.max_concurrent`，默认 3）。Swarm run 通过 `reserve_swarm_run` → `activate_swarm_run` → `_execute_subagent_with_lane` 进入 SUBAGENT lane。

**交互模型**: Swarm 的 per-group concurrency 是**第一层**（group-level admission），SUBAGENT lane 是**第二层**（global queueing）。一个 swarm run 被 activate 后进入 SUBAGENT lane 排队——如果 lane 满，swarm run 处于 PENDING 状态。Swarm 的 `_count_active_swarm_runs` 计的是 ACTIVE（per swarm），不是 RUNNING（per registry）。

**问题**: `_pump_lane` 看到 `active_count < config.max_concurrent` 就 activate 下一个 run。但如果 SUBAGENT lane 满，activated 的 run 实际是 PENDING。Swarm 以为有 N 个并发，但实际只有 M < N 个在运行。

**修复**: `_pump_lane` 需要感知 SUBAGENT lane 的排队情况。但 swarm group 的并发控制和全局 lane 是两个独立的限制——swarm 限制是 per-group，全局 lane 限制是所有 subagent 总和。两者叠加是正确的：swarm group 最多 N 个 concurrent，全局最多 M 个 concurrent，实际并发 = min(N, M - other_subagents)。

> **结论**: 无需修改 swarm collector。Swarm 的 per-group 并发 + 全局 SUBAGENT lane 是 additive 的，行为正确。Swarm activate 的 run 如果在 SUBAGENT lane 排队，只是等待更久——`_pump_lane` 不会过度 activate，因为它检查的是 swarm-level active count。

---

## 11. Main Agent Lane 集成

### 文件: `server/service/input_queue_service.py`

在 `_run_executor()` (line 277) 中包裹 lane：

```python
async def _run_executor(
    executor: TurnExecutor, session_id: str, message: str, source: Source, reply_target: str | None
) -> None:
    """Fire-and-forget wrapper: a crashing executor must not die silently."""
    from runtime.lane import lane_slot, LaneType

    async with lane_slot(LaneType.MAIN):
        try:
            await executor.execute(session_id, message, source, reply_target)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("turn executor crashed for session {}", session_id)
```

> **注意**: `submit_user_input` 的 idle 分支 `asyncio.create_task(_run_executor(...))` 不变。Lane 控制的是实际执行并发，不是提交并发。per-session 串行（`_SESSION_LOCKS` + `SessionState.detect_state`）仍有效——同一 session 的 turn 永远串行。Lane 保证的是跨 session 的全局上限。

---

## 12. Nested Lane 集成

### 文件: `agent/tools/subagent/tools/sessions_send.py`

`sessions_send._arun()` 调用 `send_subagent_message()`。后者向目标 subagent 发送消息并触发一个 turn。这个 turn 应受 NESTED lane 控制。

```python
async def _arun(self, target_session_key, message, max_turns=1):
    ...
    from runtime.lane import lane_slot, LaneType

    async with lane_slot(LaneType.NESTED):
        await send_subagent_message(
            run_id=run.run_id,
            message=message,
            caller_session_key=requester_key,
            wait_for_reply=False,
        )
    return f"Message sent to {target_session_key}"
```

> **注意**: NESTED lane 默认并发=1（串行）。这意味着同时只能有一个 sessions.send 在执行。如果多个 parent 同时 send，会排队。

---

## 13. Sessions Yield 交互

### 文件: `agent/tools/subagent/tools/sessions_yield.py`

`sessions_yield` 检查 `c.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)` (line 51-53)。

**修改**: 加入 PENDING：

```python
active = [
    c
    for c in children
    if c.execution.status in (
        ExecutionStatus.PENDING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.INTERRUPTED,
    )
]
```

> PENDING child 也是 "active"——yield 应等待它完成。如果 PENDING child 正在 SUBAGENT lane 排队，yield 会等到它获得 slot、执行完毕。yield timeout 包含 lane 等待时间——可接受的行为。

---

## 14. Server 启动初始化

### 文件: `server/__main__.py`

在 `init_trigger()` 之后、`app.start()` 之前，初始化 LaneManager：

```python
# Initialize lane manager (lazy singleton, but explicit init for clarity)
from runtime.lane import get_lane_manager

get_lane_manager()  # eagerly creates the singleton

# Start subagent sweeper (existing code, if present)
# ...

# Register shutdown handler to drain lanes
import atexit
from runtime.lane import get_lane_manager as _get_lm

async def _drain_lanes():
    await _get_lm().drain_all()

# atexit is sync — can't await. The drain happens via the sweeper's
# stop_sweeper() which is called in the existing shutdown path.
# If no async shutdown path exists, lanes are abandoned on process exit
# (acceptable — OS reclaims everything).
```

> **注意**: 如果 server 已有 async shutdown 路径（如 `stop_sweeper()`），lane drain 应在 `stop_sweeper()` 之后调用。搜索现有 shutdown 逻辑确定位置。

---

## 15. Work Admission / Graceful Shutdown

### 文件: `agent/tools/subagent/registry/work_admission.py`

现有 `work_admission` 模块管理 drain mode：

- `set_draining(True)` — 新 root work 被延迟
- `run_with_work_admission(coro)` — 检查 draining，如果在 draining 则重试

**集成**: 当 `is_gateway_draining()` 为 True 时，lane-waiting tasks 应退出而非无限等待。

### 修改: `runtime/lane/core.py`

```python
async def acquire(self, warn_ms: int = 5000) -> None:
    self._ensure_sem()
    self._queued += 1
    wait_start = time.monotonic()
    try:
        # Check drain mode before waiting
        from agent.tools.subagent.registry import is_gateway_draining
        if is_gateway_draining():
            self._queued -= 1
            raise RuntimeError("Gateway draining — lane acquire refused")
        await self._sem.acquire()
    except asyncio.CancelledError:
        self._queued -= 1
        raise
    ...
```

> **import 约束**: `runtime/lane/core.py` 不能 import `agent/`（circular）。改用 callback 或 event：

```python
# runtime/lane/core.py — 不直接 import agent
_drain_check: Callable[[], bool] | None = None

def set_drain_check(fn: Callable[[], bool] | None) -> None:
    """Register a drain-mode checker (called before lane acquire waits)."""
    global _drain_check
    _drain_check = fn
```

在 `server/__main__.py` 启动时注册:

```python
from runtime.lane.core import set_drain_check
from agent.tools.subagent.registry import is_gateway_draining

set_drain_check(is_gateway_draining)
```

---

## 16. Lane 状态 API

### 新文件: `server/trigger/lane_status.py`

```python
"""Lane status endpoint for observability."""

from . import app  # Robyn app


@app.get("/api/lane-status")
async def lane_status():
    from runtime.lane import get_lane_manager
    import json
    return json.dumps(get_lane_manager().snapshot())
```

> 在 `server/trigger/__init__.py` 中注册路由。

---

## 17. Config 校验

### 文件: `config/features/infra_side/lane_system.py`

添加校验函数（不在 import 时执行，在 server 启动时调用）：

```python
def validate_lane_config() -> None:
    """Validate lane config constraints. Called at server startup."""
    cfg = LANE_SYSTEM
    if cfg["main_max_concurrent"] < cfg["subagent_max_concurrent"] + cfg["nudge_max_concurrent"]:
        raise ValueError(
            f"MAIN lane ({cfg['main_max_concurrent']}) must be >= "
            f"SUBAGENT ({cfg['subagent_max_concurrent']}) + NUDGE ({cfg['nudge_max_concurrent']})"
        )
    if cfg["subagent_max_concurrent"] < 1:
        raise ValueError("subagent_max_concurrent must be >= 1")
    if cfg["nudge_max_concurrent"] < 1:
        raise ValueError("nudge_max_concurrent must be >= 1")
    if cfg["nested_max_concurrent"] < 1:
        raise ValueError("nested_max_concurrent must be >= 1")
```

在 `server/__main__.py` 启动时调用：

```python
from config.features.infra_side.lane_system import validate_lane_config
validate_lane_config()
```

---

## 18. 死锁防护

| 场景                                       | 机制                                                     |
| ------------------------------------------ | -------------------------------------------------------- |
| Main turn 等 subagent 结果，持有 MAIN slot | subagent `run_timeout_seconds` 超时释放                  |
| Nudge 等 SUBAGENT slot                     | Nudge 不 spawn subagent（工具集受限），不嵌套等待        |
| Lane 热更新期间 in-flight                  | `set_concurrency` 只影响新 acquire                       |
| Sessions_yield 等 PENDING subagent         | yield timeout 包含 lane 等待时间，超时后正常返回         |
| Drain mode + lane-waiting tasks            | `acquire()` 检查 drain_check，拒绝 acquire               |
| Kill PENDING run                           | `CancelledError` 从 `lane.acquire()` 传播，不释放 permit |
| Steer PENDING run                          | 拒绝（status 检查），steer 只处理 RUNNING/INTERRUPTED    |

**约束**: `main_max_concurrent >= subagent_max_concurrent + nudge_max_concurrent`，避免 main slot 被 nudge+subagent 饥饿。

---

## 19. 向后兼容

| 变更                                | 策略                                                                                                                                            |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `SubagentConfig.max_concurrent`     | 保留字段 + deprecated 注释; lane 从 `LANE_SYSTEM` 读默认                                                                                        |
| `validate_global_concurrent()`      | 保留函数 + DeprecationWarning; spawn 不再调用                                                                                                   |
| `ExecutionStatus.PENDING` 新增      | `is_live_unended_run()` / counting 函数包含 PENDING; `is_stale_unended_run()` / `reconcile_orphaned_run()` 跳过 PENDING（`started_at is None`） |
| `mark_run_running()`                | 扩展支持 PENDING→RUNNING（原仅 INTERRUPTED→RUNNING）                                                                                            |
| `delegate_task(max_concurrent=...)` | 参数保留但 warn（lane 是全局的）                                                                                                                |
| `after_agent` sync path             | nudge 调用移除（改由 `aafter_agent` async path 独占）                                                                                           |

---

## 20. 文件变更清单

| 操作 | 文件                                                        | 说明                                                                     |
| ---- | ----------------------------------------------------------- | ------------------------------------------------------------------------ |
| 新建 | `config/features/infra_side/lane_system.py`                 | Lane 配置 + 校验函数                                                     |
| 新建 | `runtime/lane/__init__.py`                                  | 包入口                                                                   |
| 新建 | `runtime/lane/core.py`                                      | Lane / LaneManager / lane_slot                                           |
| 新建 | `server/trigger/lane_status.py`                             | Lane 状态 API 端点                                                       |
| 新建 | `tests/runtime/lane/__init__.py`                            | —                                                                        |
| 新建 | `tests/runtime/lane/test_core.py`                           | Lane 基础: acquire/release/排队/计数                                     |
| 新建 | `tests/runtime/lane/test_lane_slot.py`                      | 上下文管理器: 异常释放/嵌套/unlimited                                    |
| 新建 | `tests/runtime/lane/test_manager.py`                        | 热更新/snapshot/multi-lane 隔离                                          |
| 新建 | `tests/runtime/lane/test_drain.py`                          | drain_all 超时/完成                                                      |
| 新建 | `tests/agent/tools/subagent/test_spawn_lane_integration.py` | subagent 满时排队不拒绝; PENDING→RUNNING                                 |
| 新建 | `tests/agent/tools/subagent/test_kill_pending.py`           | kill PENDING run: CancelledError 路径                                    |
| 新建 | `tests/agent/tools/subagent/test_steer_lane.py`             | steer 重启经过 lane                                                      |
| 新建 | `tests/server/service/test_main_lane.py`                    | main turn lane 排队; per-session 串行不变                                |
| 新建 | `tests/agent/middlewares/test_nudge_lane.py`                | nudge 并发受 lane 限制                                                   |
| 新建 | `tests/agent/tools/subagent/test_sweeper_pending.py`        | sweeper 对 PENDING run 的处理                                            |
| 修改 | `config/features/infra_side/__init__.py`                    | re-export LANE_SYSTEM                                                    |
| 修改 | `agent/tools/subagent/types/registry.py`                    | ExecutionStatus.PENDING 新增                                             |
| 修改 | `agent/tools/subagent/registry/run_manager.py`              | register_run: PENDING 初始; mark_run_running: PENDING→RUNNING            |
| 修改 | `agent/tools/subagent/registry/queries.py`                  | counting 函数包含 PENDING                                                |
| 修改 | `agent/tools/subagent/registry/helpers.py`                  | is_live_unended_run: 包含 PENDING                                        |
| 修改 | `agent/tools/subagent/spawn/core.py`                        | 删 validate_global_concurrent; _execute_subagent_with_lane               |
| 修改 | `agent/tools/subagent/spawn/depth.py`                       | validate_global_concurrent: deprecated                                   |
| 修改 | `agent/tools/subagent/config.py`                            | max_concurrent: deprecated 注释                                          |
| 修改 | `agent/tools/subagent/control/kill.py`                      | list_killable_children: 包含 PENDING                                     |
| 修改 | `agent/tools/subagent/control/steer.py`                     | _execute_steered_subagent_with_lane; PENDING→lane                        |
| 修改 | `agent/tools/subagent/orphan/recovery.py`                   | evaluate_recovery_gate: PENDING→wedged; _recovery_loop: PENDING finalize |
| 修改 | `agent/tools/subagent/tools/sessions_send.py`               | NESTED lane wrapping                                                     |
| 修改 | `agent/tools/subagent/tools/sessions_yield.py`              | active 检查包含 PENDING                                                  |
| 修改 | `agent/middlewares/context_engine/core.py`                  | after_agent: 移除 run_async nudge 调用                                   |
| 修改 | `server/service/input_queue_service.py`                     | _run_executor: MAIN lane wrapping                                        |
| 修改 | `server/__main__.py`                                        | LaneManager init + validate_lane_config + set_drain_check                |
| 修改 | `server/trigger/__init__.py`                                | 注册 lane_status 路由                                                    |
| 修改 | `AGENTS.md`                                                 | 架构图增加 runtime/lane/ 条目 + Lane 表                                  |
| 修改 | `docs/long-running-tasks/README.md`                         | 增加 lane 系统章节                                                       |

---

## 21. 测试计划

### 单元测试

| 文件                                   | 覆盖                                                                       |
| -------------------------------------- | -------------------------------------------------------------------------- |
| `tests/runtime/lane/test_core.py`      | acquire/release 基本操作; Semaphore 计数正确; queued/active 计数           |
| `tests/runtime/lane/test_lane_slot.py` | 异常时 release; CancelledError 不 release sem; 嵌套 lane_slot              |
| `tests/runtime/lane/test_manager.py`   | set_concurrency 热更新; snapshot 格式; 多 lane 互不干扰                    |
| `tests/runtime/lane/test_drain.py`     | drain_all 超时返回 False; drain_all 全部完成返回 True; active=0 时立即返回 |

### 集成测试

| 文件                             | 覆盖                                                                    |
| -------------------------------- | ----------------------------------------------------------------------- |
| `test_spawn_lane_integration.py` | subagent 满时排队不拒绝; PENDING→RUNNING 过渡; 多 subagent FIFO 顺序    |
| `test_kill_pending.py`           | kill PENDING run: CancelledError 从 lane.acquire 传播; PENDING→TERMINAL |
| `test_steer_lane.py`             | steer 重启经过 lane; steer PENDING run 被拒绝                           |
| `test_main_lane.py`              | main turn lane 排队; per-session 串行不变; 多 session 并发受 lane 限制  |
| `test_nudge_lane.py`             | nudge 并发受 NUDGE lane 限制; 超过 4 个时排队                           |
| `test_sweeper_pending.py`        | sweeper 对 PENDING run 的处理; orphan PENDING → finalize                |
| `test_sessions_yield_pending.py` | yield 等待 PENDING child; timeout 包含 lane 等待时间                    |

### 回归测试

| 场景                                 | 验证                                                        |
| ------------------------------------ | ----------------------------------------------------------- |
| `validate_global_concurrent()` 调用  | DeprecationWarning 但不报错                                 |
| `SubagentConfig.max_concurrent` 读取 | 仍可读取，值不变                                            |
| `count_all_active_runs()` 语义变化   | 原来只计 RUNNING，现在计 RUNNING+PENDING — 需检查所有调用方 |
| `is_live_unended_run()` 语义变化     | 原来不含 PENDING，现在含 — 需检查所有调用方                 |

### 需检查的调用方

`count_all_active_runs()` / `count_active_runs_for_session()` / `count_active_descendant_runs()` 的所有调用方（语义变化可能影响逻辑）:

- `spawn/depth.py:validate_global_concurrent` — deprecated, 不再调用
- `spawn/depth.py:validate_concurrent_children` — 保留, PENDING child 应计入 per-parent budget
- `registry/read.py` — readonly 转发, 自动继承
- `swarm/collector.py` — 检查是否使用 counting 函数
- `orphan/recovery.py` — `scan_orphaned_sessions` 使用 `is_live_unended_run`

`is_live_unended_run()` 的所有调用方:

- `orphan/recovery.py:scan_orphaned_sessions` — 应包含 PENDING
- `orphan/recovery.py:_recovery_loop` — 检查 `is_live_unended_run` 后进入 recovery
- `registry/helpers.py:should_keep_child_link` — PENDING child link 应保留
- `registry/sweeper.py:_scan_stale_waiting_taskflows` — child 活跃度检查

---

## 实现顺序（推荐）

1. **Step 3**: 新建 config + runtime/lane/ 核心文件
2. **Step 4**: PENDING 状态 + run_manager 修改
3. **Step 6**: Registry counting 函数更新 + helpers.py
4. **Step 5**: Subagent lane 集成 (spawn/core.py + depth.py + steer.py)
5. **Step 7**: Kill/cancel 处理 (kill.py)
6. **Step 8**: Sweeper/orphan recovery 更新
7. **Step 9**: run_async() 兼容性 (context_engine/core.py)
8. **Step 11**: Main agent lane (input_queue_service.py)
9. **Step 12**: Nested lane (sessions_send.py)
10. **Step 13**: Sessions yield (sessions_yield.py)
11. **Step 15**: Work admission 集成
12. **Step 14**: Server 启动初始化
13. **Step 16-17**: API 端点 + config 校验
14. **Step 21**: 测试
15. **Step 20**: 文档更新
