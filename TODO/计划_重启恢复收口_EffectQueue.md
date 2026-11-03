# 计划 — 重启恢复收口（EffectQueue + 终态声明表）

> 目标：将 sherry 散装的重启恢复路径收口为"意图日志 + 终态声明"两层架构。
> **铁律：绝不能影响现有功能。** 每一步都是 additive——新模块旁路运行、feature flag 门控、
> 旧 sweeper 在新路径证明等价后才退役。任何阶段失败都能回退到当前行为。

---

## 设计原则

1. **Additive 优先**：新模块（`runtime/effects/`）独立存在，不修改任何现有模块的代码路径。
2. **Feature flag 门控**：开关在 `sherry.jsonc` 的 `effects.*`，默认全 `false`。
3. **Shadow mode**：新路径先与旧路径并行（只记录不执行），对比一致后才切执行。
4. **旧 sweeper 保留**：新路径上线后旧 sweeper 不删，降级为兜底。
5. **Seam 契约不变**：`runtime/hooks.py` 的 `MAYBE_TRIGGER_AUTO_TURN` / `AUTO_TURN_MODULE` /
   `WS_ACTIVE_TASKS` 名称和语义不变。
6. **DB additive**：新表独立文件 `runtime/effects/effects.db`，不改现有表结构。

---

## 新建文件结构

```
runtime/effects/
├── __init__.py            # 公共 API 导出
├── intent_log.py          # 意图表 SQLite store + 入队/重放 API
├── worker.py              # worker 循环 + handler 注册
├── restart_table.py       # 终态声明表
├── config.py              # effects 配置读取（从 sherry.jsonc）
└── handlers.py            # 三条路径的 handler 实现（Phase 2）

tests/runtime/effects/
├── __init__.py
├── test_intent_log.py     # 意图表 CRUD + upsert + generation + replay
├── test_worker.py         # worker 执行/退避/重试/放弃
├── test_restart_table.py  # 终态声明表
├── test_shadow_continuation.py   # Phase 1 影子对比
├── test_shadow_completion.py     # Phase 1 影子对比
├── test_worker_continuation.py   # Phase 2 worker 执行
├── test_worker_completion.py     # Phase 2 worker 执行
└── test_restart_replay.py        # Phase 3 重启重放
```

---

## Phase 0 — 基础设施（纯新增，零影响）

### Step 0.1 配置注册

**文件**：`config/sherry_settings.py`

在 `SHERRY_SETTING_DEFAULTS`（L47-91）中追加：
```python
"effects.enabled": False,           # 总开关
"effects.shadow_mode": False,      # 影子模式（只记录不执行）
"effects.worker_enabled": False,   # worker 是否启动
"effects.restart_replay": False,   # 重启时是否重放意图表
"effects.poll_interval_s": 2,      # worker 轮询间隔
"effects.retry_initial_s": 5,      # 退避初始间隔
"effects.retry_max_s": 300,        # 退避上限
"effects.max_attempts": 10,        # 最大重试次数
```

在 `_BOOL_KEYS`（L106-115）中追加 `"effects.enabled"`, `"effects.shadow_mode"`,
`"effects.worker_enabled"`, `"effects.restart_replay"`。
在 `_INT_KEYS`（L95-104）中追加 `"effects.poll_interval_s"`, `"effects.retry_initial_s"`,
`"effects.retry_max_s"`, `"effects.max_attempts"`。

**读取入口**：新建 `runtime/effects/config.py`
```python
from config.sherry_settings import get_sherry_setting

def effects_enabled() -> bool:
    return bool(get_sherry_setting("effects.enabled", False))

def shadow_mode() -> bool:
    return bool(get_sherry_setting("effects.shadow_mode", False))

def worker_enabled() -> bool:
    return bool(get_sherry_setting("effects.worker_enabled", False))

def restart_replay() -> bool:
    return bool(get_sherry_setting("effects.restart_replay", False))

def poll_interval_s() -> int:
    return int(get_sherry_setting("effects.poll_interval_s", 2))

def retry_initial_s() -> int:
    return int(get_sherry_setting("effects.retry_initial_s", 5))

def retry_max_s() -> int:
    return int(get_sherry_setting("effects.retry_max_s", 300))

def max_attempts() -> int:
    return int(get_sherry_setting("effects.max_attempts", 10))
```

**不改 sherry.jsonc 文件本身**——用户不改配置时默认值生效，行为不变。

### Step 0.2 意图表 store

**文件**：`runtime/effects/intent_log.py`

继承 `BaseSQLiteRepository`（`agent/tools/pub_base/sqlite_store.py` L43）。
按照 BaseSQLiteRepository 的 namespace 模式：

```python
import asyncio, json, threading, time
from pathlib import Path
from config.path import APP_HOME
from agent.tools.pub_base.sqlite_store import BaseSQLiteRepository

_DB_DIR = Path(APP_HOME) / "runtime" / "effects"
_DB_PATH = _DB_DIR / "effects.db"
_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_S = 5.0
_INIT_WAIT_TIMEOUT_S = 10.0
_initialized = False
_init_loop: asyncio.AbstractEventLoop | None = None
_init_lock = asyncio.Lock()
_sync_tables_ready = False
_sync_init_lock = threading.Lock()

_EFFECT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS effect_intent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 1,
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    due_at      REAL    NOT NULL,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    UNIQUE(kind, key)
)
"""

_EFFECT_INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_effect_intent_status_due ON effect_intent(status, due_at)",
)

class _EffectStore(BaseSQLiteRepository):
    wal_label = "effects store"

    def _table_ddls(self) -> tuple[str, ...]:
        return (_EFFECT_TABLE_DDL,)

    def _index_ddls(self) -> tuple[str, ...]:
        return _EFFECT_INDEX_DDLS

_store: _EffectStore | None = None

def _get_store() -> _EffectStore:
    global _store
    if _store is None:
        _store = _EffectStore(globals())
    return _store
```

**入队/标记 API**（同步 sqlite3，不走 async ensure_db——这些是 boot 和 worker 内部调用）：

```python
from dataclasses import dataclass

@dataclass
class IntentRow:
    id: int
    kind: str
    key: str
    payload: dict
    generation: int
    status: str
    attempts: int
    due_at: float

def enqueue(kind: str, key: str, payload: dict, due_at: float | None = None) -> tuple[int, int]:
    """Upsert by (kind, key). Returns (intent_id, generation)."""
    now = time.monotonic()
    due = due_at if due_at is not None else now
    payload_json = json.dumps(payload, ensure_ascii=False)
    # INSERT ON CONFLICT upsert
    _exec_sync(
        """INSERT INTO effect_intent (kind, key, payload, generation, status, attempts, due_at, created_at, updated_at)
           VALUES (?, ?, ?, 1, 'pending', 0, ?, ?, ?)
           ON CONFLICT(kind, key) DO UPDATE SET
             payload = excluded.payload,
             generation = generation + 1,
             status = 'pending',
             attempts = 0,
             due_at = excluded.due_at,
             updated_at = excluded.updated_at""",
        (kind, key, payload_json, due, now, now, now),
    )
    row = _query_sync(
        "SELECT id, generation FROM effect_intent WHERE kind = ? AND key = ?",
        (kind, key),
    )
    return row[0][0], row[0][1]

def mark_in_flight(intent_id: int) -> bool:
    """CAS: pending → in_flight. Returns True if acquired."""
    _exec_sync(
        "UPDATE effect_intent SET status = 'in_flight', updated_at = ? WHERE id = ? AND status = 'pending'",
        (time.monotonic(), intent_id),
    )
    return _query_sync("SELECT changes()")[0][0] > 0

def mark_done(intent_id: int) -> None:
    _exec_sync(
        "UPDATE effect_intent SET status = 'done', updated_at = ? WHERE id = ?",
        (time.monotonic(), intent_id),
    )

def mark_retry(intent_id: int, next_due_at: float) -> None:
    _exec_sync(
        "UPDATE effect_intent SET status = 'pending', attempts = attempts + 1, due_at = ?, updated_at = ? WHERE id = ?",
        (next_due_at, time.monotonic(), intent_id),
    )

def mark_abandoned(intent_id: int) -> None:
    _exec_sync(
        "UPDATE effect_intent SET status = 'abandoned', updated_at = ? WHERE id = ?",
        (time.monotonic(), intent_id),
    )

def fetch_pending(limit: int = 50) -> list[IntentRow]:
    """取 due_at <= now 的 pending 行，按 due_at 排序。"""
    now = time.monotonic()
    rows = _query_sync(
        """SELECT id, kind, key, payload, generation, status, attempts, due_at
           FROM effect_intent
           WHERE status = 'pending' AND due_at <= ?
           ORDER BY due_at ASC
           LIMIT ?""",
        (now, limit),
    )
    return [IntentRow(id=r[0], kind=r[1], key=r[2], payload=json.loads(r[3]),
                      generation=r[4], status=r[5], attempts=r[6], due_at=r[7]) for r in rows]

def replay_pending() -> list[IntentRow]:
    """重启恢复：重置 in_flight → pending，rebase due_at，返回待重放列表。"""
    now = time.monotonic()
    _exec_sync(
        """UPDATE effect_intent
           SET status = 'pending', due_at = ?, updated_at = ?
           WHERE status = 'in_flight'""",
        (now, now),
    )
    # 已 pending 且 due_at 在过去/未来的保持原 due_at（相对间隔不变）
    return fetch_pending(limit=1000)
```

`_exec_sync` / `_query_sync` 用 `sqlite3.connect(str(_DB_PATH))` + WAL pragma + busy_timeout。
同步调用，不走 `ensure_db()`（避免 async loop 依赖）。表创建用
`_sync_init` 路径（`_sync_tables_ready` + `_sync_init_lock`）。

### Step 0.3 worker 循环

**文件**：`runtime/effects/worker.py`

```python
import asyncio, time, logging
from dataclasses import dataclass
from typing import Callable, Awaitable
from runtime.effects import intent_log
from runtime.effects.config import poll_interval_s, retry_initial_s, retry_max_s, max_attempts

logger = logging.getLogger(__name__)

@dataclass
class EffectResult:
    done: bool = False
    retry_after_s: float | None = None
    abandon: bool = False

Handler = Callable[[dict], Awaitable[EffectResult]]

class EffectWorker:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._task: asyncio.Task | None = None
        self._stop = False

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop:
            try:
                await self._tick()
            except Exception:
                logger.exception("effect worker tick failed")
            await asyncio.sleep(poll_interval_s())

    async def _tick(self) -> None:
        pending = intent_log.fetch_pending(limit=50)
        for row in pending:
            handler = self._handlers.get(row.kind)
            if handler is None:
                continue
            if not intent_log.mark_in_flight(row.id):
                continue  # 被其他 worker 抢走（多 worker 时）
            try:
                result = await handler(row.payload)
                if result.done:
                    intent_log.mark_done(row.id)
                elif result.abandon or row.attempts >= max_attempts():
                    intent_log.mark_abandoned(row.id)
                elif result.retry_after_s is not None:
                    delay = min(result.retry_after_s, retry_max_s())
                    intent_log.mark_retry(row.id, time.monotonic() + delay)
                else:
                    # 默认退避
                    delay = min(retry_initial_s() * (2 ** row.attempts), retry_max_s())
                    intent_log.mark_retry(row.id, time.monotonic() + delay)
            except Exception:
                logger.exception("effect handler failed for kind=%s id=%d", row.kind, row.id)
                delay = min(retry_initial_s() * (2 ** row.attempts), retry_max_s())
                intent_log.mark_retry(row.id, time.monotonic() + delay)

_worker: EffectWorker | None = None

def get_worker() -> EffectWorker:
    global _worker
    if _worker is None:
        _worker = EffectWorker()
    return _worker
```

### Step 0.4 终态声明表

**文件**：`runtime/effects/restart_table.py`

```python
from dataclasses import dataclass, field

@dataclass
class RestartTerminal:
    """声明一个状态机的重启终态强制规则。"""
    name: str                           # 人类可读名
    apply_fn: callable                  # 实际执行函数（Phase 3 实现）
    non_terminals: set[str]             # 需要强制的非终态集合
    force_to: str                       # 强制转成的终态
    extra: dict[str, str] = field(default_factory=dict)

# Phase 0 只声明，apply_fn 为 no-op。Phase 3 填充实际执行函数。
_RESTART_TERMINALS: list[RestartTerminal] = []

def register_restart_terminal(spec: RestartTerminal) -> None:
    _RESTART_TERMINALS.append(spec)

def all_terminals() -> list[RestartTerminal]:
    return list(_RESTART_TERMINALS)
```

### Step 0.5 测试

**文件**：`tests/runtime/effects/test_intent_log.py`

```python
# 测试用例：
# 1. test_enqueue_insert: 新 kind+key → 插入，generation=1
# 2. test_enqueue_upsert: 同 kind+key 二次入队 → generation=2, payload 替换, attempts 重置
# 3. test_mark_in_flight_cas: pending→in_flight 成功；再次 mark 同 id → 失败
# 4. test_mark_done: in_flight→done
# 5. test_mark_retry: attempts+1, due_at 更新
# 6. test_mark_abandoned: 超过 max_attempts → abandoned
# 7. test_fetch_pending: 只取 due_at<=now 的 pending 行，按 due_at 排序
# 8. test_replay_pending: in_flight→pending 重置；pending 保持
# 9. test_generation_not_overwritten: 旧 in_flight 完成后 mark_done，
#    但新 generation 已入队 → 旧 done 不影响新 pending
```

**文件**：`tests/runtime/effects/test_worker.py`

```python
# 测试用例：
# 1. test_handler_done: handler 返回 done → intent 标 done
# 2. test_handler_retry: handler 返回 retry_after_s → intent 标 pending+attempts+1
# 3. test_handler_abandon: attempts >= max → abandoned
# 4. test_handler_exception: handler 抛异常 → 退避重试
# 5. test_concurrent_acquire: 两个 worker 抢同一 intent → 只一个执行
```

### Phase 0 验收标准

- [ ] `runtime/effects/` 模块存在，`test_intent_log.py` + `test_worker.py` 全部通过
- [ ] `sherry_settings.py` 追加了 `effects.*` 键，默认全 False
- [ ] `grep -r "effects" agent/ server/` 确认无任何现有模块引用 `runtime/effects`
- [ ] **`sherry.jsonc` 不含 `effects` 配置时 `run_tests_split.py` 零回归**

---

## Phase 1 — 影子观察（旁路记录，不执行）

**前置条件**：Phase 0 验收通过。
**关键：shadow mode 下意图表只写不执行，worker 不启动。**

### Step 1.1 continuation 影子记录

**插入点**：`agent/middlewares/todo_continuation/core.py` 的 `_inject()` 方法

在 L558（`if trigger is None: return False` 之后、L562 `result = await trigger(...)` 之前）插入：

```python
_shadow_intent_id: int | None = None
if shadow_mode():
    from runtime.effects import intent_log
    _shadow_intent_id, _ = intent_log.enqueue(
        kind="continuation",
        key=f"{session_id}:{st.turn_count(session_id)}:{hash(prompt)}",
        payload={"session_key": session_key, "prompt": prompt, "origin": _ORIGIN},
    )
```

在 L566 `_trigger_refused(result)` 分支（L571-576）内追加：
```python
if _shadow_intent_id is not None:
    intent_log.mark_retry(_shadow_intent_id, time.monotonic() + 5)
```

在 L577 `if count_turn:` 分支（L578 `st.mark_injected`）之后追加：
```python
if _shadow_intent_id is not None:
    intent_log.mark_done(_shadow_intent_id)
```

**不改原逻辑**：所有 `if shadow_mode():` 块都是旁路，flag 关闭时不执行。

### Step 1.2 completion 影子记录

**插入点**：`agent/tools/subagent/announce/delivery.py` 的 `deliver_subagent_announcement`（L311）

在 L313（`config = get_config()` 之后、L315 `_is_already_delivered` 之前）插入：
```python
_shadow_intent_id: int | None = None
if shadow_mode():
    from runtime.effects import intent_log
    _shadow_intent_id, _ = intent_log.enqueue(
        kind="completion_notice",
        key=f"run:{run.run_id}",
        payload={"run_id": run.run_id, "session_key": run.session_key,
                 "result_text": run.completion.result_text[:24000] if run.completion.result_text else ""},
    )
```

在函数成功返回前（`return AnnounceDeliveryResult(success=True)` 或末尾 return）追加：
```python
if _shadow_intent_id is not None:
    intent_log.mark_done(_shadow_intent_id)
```

在 except 块内追加：
```python
if _shadow_intent_id is not None:
    intent_log.mark_retry(_shadow_intent_id, time.monotonic() + 5)
```

### Step 1.3 auto-turn 影子记录

**插入点**：`server/service/auto_turn.py` 的 `maybe_trigger_auto_turn`（L91）

在 L100（`bare = normalize_session_key(session_key)` 之后）插入：
```python
_shadow_intent_id: int | None = None
if shadow_mode():
    from runtime.effects import intent_log
    _shadow_intent_id, _ = intent_log.enqueue(
        kind="auto_turn",
        key=f"turn:{bare}",
        payload={"session_key": session_key, "injection_content": str(injection.content)[:500]},
    )
```

在函数 return 之前（根据 `AutoTurnResult` 的 outcome）追加：
```python
if _shadow_intent_id is not None:
    if result.outcome == AutoTurnOutcome.TRIGGERED:
        intent_log.mark_done(_shadow_intent_id)
    else:
        intent_log.mark_retry(_shadow_intent_id, time.monotonic() + 5)
```

### Step 1.4 对比验证脚本

**文件**：`scripts/verify_effects_shadow.py`

```python
# 对比日志输出：
# 1. continuation: 意图表 done 行数 vs st.mark_injected 计数 → 差值 = busy 计数偏差
# 2. completion: 意图表 done 行 vs registry delivery DELIVERED 行 → 差值 = sweeper 漏投
# 3. auto_turn: 意图表 done 行 vs _INFLIGHT 已完成数 → 差值 = 内存态丢失
```

### Phase 1 验收标准

- [ ] `effects.shadow_mode = True` 时三条路径旁路记录，原有逻辑完全不变
- [ ] `run_tests_split.py` 零回归（shadow flag 关闭时）
- [ ] shadow 开启后手动冒烟：触发续作 → 意图表有记录；spawn subagent 完成 → 意图表有记录
- [ ] `verify_effects_shadow.py` 输出一致率 ≥ 99%
- [ ] **`effects.shadow_mode = False` 后行为与当前完全一致**

---

## Phase 2 — 新路径执行（worker 接管，旧路径保留）

**前置条件**：Phase 1 对比一致率 ≥ 99%。

### Step 2.1 handler 实现

**文件**：`runtime/effects/handlers.py`

```python
import time, logging
from runtime.effects.worker import EffectResult
from runtime.effects import intent_log
from runtime.hooks import resolve as resolve_hook
import runtime.hooks as hooks
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

_ORIGIN = "todo_continuation"  # 与 core.py 的 _ORIGIN 一致

async def handle_continuation(payload: dict) -> EffectResult:
    """continuation handler: 调用 auto-turn trigger 投递续作指令。"""
    trigger = resolve_hook(hooks.MAYBE_TRIGGER_AUTO_TURN)
    if trigger is None:
        return EffectResult(abandon=True)
    injection = HumanMessage(
        content=payload["prompt"],
        metadata={"origin": _ORIGIN, "internal": True},
    )
    try:
        result = await trigger(payload["session_key"], injection)
        # _trigger_refused 等价判断
        outcome = getattr(result, "outcome", None)
        if outcome is not None and outcome != "triggered":
            return EffectResult(retry_after_s=5)
        return EffectResult(done=True)
    except Exception:
        logger.exception("continuation handler failed")
        return EffectResult(retry_after_s=5)

async def handle_completion_notice(payload: dict) -> EffectResult:
    """completion handler: 调用现有 announce 投递。"""
    from agent.tools.subagent.registry.run_manager import get_run
    from agent.tools.subagent.announce.delivery import deliver_subagent_announcement
    run = get_run(payload["run_id"])
    if run is None:
        return EffectResult(abandon=True)  # run 已被清理
    try:
        result = await deliver_subagent_announcement(run)
        if result.success:
            return EffectResult(done=True)
        return EffectResult(retry_after_s=5)
    except Exception:
        logger.exception("completion handler failed")
        return EffectResult(retry_after_s=5)

async def handle_auto_turn(payload: dict) -> EffectResult:
    """auto-turn handler: 调用 maybe_trigger_auto_turn。"""
    from server.service.auto_turn import maybe_trigger_auto_turn, AutoTurnOutcome
    injection = HumanMessage(content=payload["injection_content"])
    try:
        result = await maybe_trigger_auto_turn(payload["session_key"], injection)
        if result.outcome == AutoTurnOutcome.TRIGGERED:
            return EffectResult(done=True)
        return EffectResult(retry_after_s=5)
    except Exception:
        logger.exception("auto_turn handler failed")
        return EffectResult(retry_after_s=5)
```

### Step 2.2 worker 启动 + handler 注册

**插入点**：`server/__main__.py` 的 boot 序列

在 L256（`reconcile_at_boot` 的 try/except 之后）、L258（`install_lane_lifecycle` import 之前）插入：

```python
# Effect worker: 仅当 effects.worker_enabled 时启动
from runtime.effects.config import worker_enabled, effects_enabled
if worker_enabled() and effects_enabled():
    from runtime.effects.worker import get_worker
    from runtime.effects.handlers import (
        handle_continuation, handle_completion_notice, handle_auto_turn,
    )
    worker = get_worker()
    worker.register("continuation", handle_continuation)
    worker.register("completion_notice", handle_completion_notice)
    worker.register("auto_turn", handle_auto_turn)
    worker.start()
```

### Step 2.3 三条路径切换为新路径

**continuation**（`agent/middlewares/todo_continuation/core.py::_inject`）：

在 L558 之后，将 shadow 分支升级为执行分支：
```python
if effects_enabled() and not shadow_mode():
    # 新路径：只入队，worker 执行
    intent_log.enqueue(
        kind="continuation",
        key=f"{session_id}:{st.turn_count(session_id)}:{hash(prompt)}",
        payload={"session_key": session_key, "prompt": prompt},
    )
    return True  # 不调 trigger，worker 会调
elif shadow_mode():
    # Phase 1 的影子逻辑保留
    ...
# else: 原有逻辑（flag 全关时）
result = await trigger(...)
```

**completion**（`agent/tools/subagent/announce/delivery.py::deliver_subagent_announcement`）：

在 L313 之后：
```python
if effects_enabled() and not shadow_mode():
    from runtime.effects import intent_log
    intent_log.enqueue(
        kind="completion_notice",
        key=f"run:{run.run_id}",
        payload={"run_id": run.run_id, "session_key": run.session_key,
                 "result_text": run.completion.result_text[:24000] if run.completion.result_text else ""},
    )
    return AnnounceDeliveryResult(success=True)  # worker 会投递
elif shadow_mode():
    # Phase 1 的影子逻辑保留
    ...
# else: 原有逻辑（flag 全关时）
```

**auto-turn**（`server/service/auto_turn.py::maybe_trigger_auto_turn`）：

在 L100 之后：
```python
if effects_enabled() and not shadow_mode():
    from runtime.effects import intent_log
    intent_log.enqueue(
        kind="auto_turn",
        key=f"turn:{bare}",
        payload={"session_key": session_key, "injection_content": str(injection.content)[:500]},
    )
    return AutoTurnResult(outcome=AutoTurnOutcome.TRIGGERED, session_key=bare, reason="effects_queue")
elif shadow_mode():
    # Phase 1 的影子逻辑保留
    ...
# else: 原有逻辑（flag 全关时）
```

**旧路径保留**：`_scan_undelivered_terminal_runs`、`mark_injected`、`_INFLIGHT` 都不变。
新路径投递后旧 sweeper 发现 delivery 已 DELIVERED 就跳过——现有幂等逻辑已处理。

### Step 2.4 测试

**文件**：`tests/runtime/effects/test_worker_continuation.py`
```python
# 1. test_continuation_worker_executes: 入队 → worker 执行 → intent done
# 2. test_continuation_worker_retry: trigger 返回 busy → retry
# 3. test_continuation_worker_abandon: trigger 不可用 → abandon
# 4. test_continuation_old_path_preserved: flag 关闭 → 走原 trigger 逻辑
```

**文件**：`tests/runtime/effects/test_worker_completion.py`
```python
# 1. test_completion_worker_executes: 入队 → worker 投递 → intent done
# 2. test_completion_worker_retry: deliver 抛异常 → retry
# 3. test_completion_idempotent: 旧 sweeper 和 worker 都投递 → 不重复（registry 幂等）
```

### Phase 2 验收标准

- [ ] `effects.enabled=True, worker_enabled=True` 时三条路径由 worker 执行
- [ ] `effects.enabled=False` 时走旧逻辑，`run_tests_split.py` 零回归
- [ ] 双写期间无重复投递（registry/delivery 幂等生效）
- [ ] 手动测试：continuation busy → 退避重试（意图表可见 retry 记录）
- [ ] 手动测试：subagent 完成 → worker 投递通知
- [ ] **`effects.enabled=False` 后行为与当前完全一致**

---

## Phase 3 — 重启重放 + 终态声明执行

**前置条件**：Phase 2 稳定运行。

### Step 3.1 重启意图重放

**插入点**：`server/__main__.py` boot 序列

在 L256（`reconcile_at_boot` 之后）插入：
```python
if restart_replay():
    from runtime.effects import intent_log
    from runtime.effects.config import worker_enabled, effects_enabled
    pending = intent_log.replay_pending()  # 重置 in_flight→pending, rebase due_at
    logger.info("effects replay: {} pending intents", len(pending))
    if worker_enabled() and effects_enabled():
        # worker 已在 Phase 2 启动，会自动取 pending 行执行
        pass
    else:
        # worker 未启动时，pending 行等 worker 下次启动时执行
        logger.info("effects worker not enabled; pending intents will execute on next worker start")
```

### Step 3.2 终态声明表执行

**填充** `runtime/effects/restart_table.py` 的 `apply_fn`：

```python
def apply_subagent_terminals() -> None:
    """强制非终态 subagent run 转为终态。

    直接调用 registry.state.restore_runs_from_disk()——它已做完整重启恢复：
    PENDING→TIMEOUT/pending_orphaned、RUNNING→TIMEOUT/process_restarted、
    INTERRUPTED 处理、monotonic 时间戳 rebase、delivery 保持 PENDING。
    幂等：restore_runs_from_disk 的 _restore_done 进程级守卫（state.py L27）
    确保每进程只执行一次，第二次调用是 no-op。
    """
    from agent.tools.subagent.registry.state import restore_runs_from_disk
    restore_runs_from_disk()


def apply_queue_terminals() -> None:
    """强制 CLAIMED 队列行回到 QUEUED。

    幂等：UPDATE ... WHERE status='CLAIMED'，没有 CLAIMED 行时影响 0 行。
    """
    from server.queue.user_input_queue import UserInputQueue
    UserInputQueue.requeue_claimed()
```

**为什么直接调用现有函数而非重新实现**：

`restore_runs_from_disk` 做的事比"纯终态强制"更多（时间戳 rebase、delivery 保持 PENDING、
settle-wake state 恢复）——但全是正确的重启恢复操作。重新实现一个"只做终态强制"的版本会：
1. 重复 `state.py::_mark_restored_running_terminal`（L100-123）和
   `_finalize_restored_pending`（L30-50）的 pydantic model_copy 转换逻辑
2. 需要独立处理 `ExecutionState` / `CompletionState` / `CompletionDeliveryState`
3. 和 `restore_runs_from_disk` 的 `_restore_done` 守卫冲突（两路径各跑一次 rebase）

声明表的价值不在于"重新实现恢复逻辑"，而在于：
- **集中可见性**：所有需重启恢复的状态机在一处声明，新增时必须在此注册
- **启动检查**：boot 时检查所有有 status 的表是否在声明表中注册（未注册 → warn）
- **未来扩展**：新增状态机如果恢复逻辑简单（一条 UPDATE），可在声明表中直接实现

**注册**（Phase 0 的声明表填实际 apply_fn）：
```python
register_restart_terminal(RestartTerminal(
    name="subagent_runs",
    apply_fn=apply_subagent_terminals,
    non_terminals={"PENDING", "RUNNING", "INTERRUPTED"},
    force_to="TIMEOUT",
    extra={"pause_reason": "process_restart", "ended_reason": "process_restarted"},
))
register_restart_terminal(RestartTerminal(
    name="user_input_queue",
    apply_fn=apply_queue_terminals,
    non_terminals={"CLAIMED"},
    force_to="QUEUED",
))
```

**执行点**：`server/__main__.py` boot 序列，在 `reconcile_at_boot` 之前：
```python
if restart_replay():
    from runtime.effects.restart_table import all_terminals
    for spec in all_terminals():
        try:
            spec.apply_fn()
        except Exception:
            logger.exception("restart terminal apply failed: %s", spec.name)
```

**关键：直接调用 `restore_runs_from_disk` 和 `requeue_claimed`，不重新实现**。
两者都有幂等守卫（`_restore_done` 进程级 no-op / SQL WHERE 条件），声明表执行和
`reconcile_at_boot` 重复调用不冲突。

### Step 3.3 due_at rebase

`intent_log.replay_pending()` 已经在内部 rebase `in_flight` 行的 `due_at` 到 `now`。
已 pending 且 `due_at` 在未来的行保持原值（相对间隔不变）。
已 pending 且 `due_at` 在过去的行保持原值（立即到期重放）。

这和 `_rebase_restored_clock` 的语义一致——monotonic 时间戳在重启后参照系变了，
但意图表的 `due_at` 是"相对偏移"语义（入队时 = `now + delay`），重启后 `now` 变了，
过期的立即执行，未来的保持间隔。

### Step 3.4 测试

**文件**：`tests/runtime/effects/test_restart_replay.py`
```python
# 1. test_replay_resets_in_flight: 重启前有 in_flight 行 → replay 后变 pending
# 2. test_replay_preserves_pending: pending 行保持 pending，due_at 不变
# 3. test_replay_overdue_immediate: due_at 在过去 → fetch_pending 立即返回
# 4. test_restart_terminal_subagent: 非终态 run 被强制转为 TIMEOUT
# 5. test_restart_terminal_queue: CLAIMED 行被转为 QUEUED
# 6. test_restart_idempotent_with_existing: 终态强制与 restore_runs_from_disk 幂等
# 7. test_restart_flag_off: restart_replay=False → 走旧 boot recovery，行为不变
```

### Phase 3 验收标准

- [ ] `restart_replay=True` 时重启后意图表 pending 行被重放
- [ ] 终态声明表执行结果与 `restore_runs_from_disk` + `requeue_claimed` 一致（幂等）
- [ ] `restart_replay=False` 时走旧 boot recovery，`run_tests_split.py` 零回归
- [ ] 手动测试：continuation 入队 → 重启 → worker 重放投递
- [ ] 手动测试：subagent 完成 → 重启 → 通知重放投递

---

## 不变保证清单（每 phase 必须满足）

| 保证项 | 验证方式 |
|---|---|
| `effects.*` flag 全 False 时行为完全一致 | `run_tests_split.py` 零回归 |
| `runtime/hooks.py` seam 名称和语义不变 | `grep -rn "MAYBE_TRIGGER_AUTO_TURN\|AUTO_TURN_MODULE\|WS_ACTIVE_TASKS" runtime/` 确认无改动 |
| 现有 SQLite 表结构不变 | 新表在独立文件 `effects.db` |
| `boot_recovery.py` / `restore_runs_from_disk` / sweeper 逻辑不被修改 | git diff 确认 |
| `PlanContinuationEnforcer._inject` 在 flag 关闭时不变 | git diff 确认只在 `if effects_enabled()` 分支内新增 |
| `announce/delivery.py` 在 flag 关闭时不变 | git diff 确认 |
| `auto_turn.py` 在 flag 关闭时不变 | git diff 确认 |
| lane / turn_runner / input_queue 不变 | git diff 确认 |

---

## 实施顺序与依赖

```
Phase 0 (新增模块 + 测试)
  │  不依赖任何现有代码
  │  验收: 单测通过 + grep 确认无引用
  ▼
Phase 1 (影子记录)
  │  依赖 Phase 0
  │  在 3 个文件各插入 ~10 行 if shadow_mode() 旁路
  │  验收: 对比一致率 ≥ 99% + 全量测试零回归
  ▼
Phase 2 (worker 执行)
  │  依赖 Phase 1 验收通过
  │  新建 handlers.py + 3 个文件 flag 分支升级 + boot 注册
  │  验收: 手动冒烟 + 全量测试零回归
  ▼
Phase 3 (重启重放)
  │  依赖 Phase 2 稳定
  │  boot 序列插入重放 + 终态声明执行
  │  验收: 重启冒烟 + 全量测试零回归
  ▼
Phase 4 (收尾简化，独立决策)
     不在本计划内；旧 sweeper 保留
```

任何 phase 失败 → `effects.*` flag 全设 False → 回退到当前行为。
