# 03 Engine：五部件、事务、角色接口

复刻原型 `src/core/engine.py` + `007-architecture.md §Engine`。Engine 是 Trigger/Thread/Decision/Ticket/Assignment 的**唯一 writer**，全同步，每命令一事务。

## 1. 端口（对应原型 `src/core/ports.py`）

```python
# superagent/core/ports.py
from typing import Protocol, Optional
from superagent.core.model import Trigger, Decision, Ticket, Report, Receipt

class Repositories(Protocol):
    """原子事务 + 各仓库的 Pick"""
    @property
    def atomic(self) -> "Atomic": ...
    @property
    def triggers(self) -> "TriggerRepo": ...
    @property
    def threads(self) -> "ThreadRepo": ...
    @property
    def tickets(self) -> "TicketRepo": ...
    @property
    def decisions(self) -> "DecisionRepo": ...

class Atomic(Protocol):
    def __enter__(self) -> "Atomic": ...   # BEGIN
    def __exit__(self, *exc) -> None: ...  # COMMIT/ROLLBACK
    def after_commit(self, fn) -> None: ...

class Policy(Protocol):
    @property
    def version(self) -> str: ...
    @property
    def paused(self) -> bool: ...
    @property
    def eval_timeout_ms(self) -> int: ...
    def gate(self, trigger, thread, tickets) -> "GateResult": ...
    def revalidate(self, mandate) -> bool: ...
    def admits_inferred(self, rules) -> bool: ...

class SourceScope(Protocol):
    def receive(self, trigger: Trigger) -> str: ...        # stored/duplicate/filtered
    def bind_thread(self, ticket_id, thread, own: bool) -> None: ...
    def thread_closed(self, thread, outcome, at) -> None: ...

class Assessor(Protocol):
    def request(self, eval_id: str, trigger_ids: list[str]) -> None: ...
    def release(self, eval_id: str) -> None: ...

class Executor(Protocol):
    def start(self, ticket_id, assignment_id) -> None: ...
    def deliver(self, ticket_id, assignment_id, input_ids) -> None: ...
    def stop(self, ticket_id) -> None: ...

class Outbox(Protocol):
    def send(self, message: Receipt) -> None: ...
    def status(self, receipt_id: str) -> Optional[str]: ...
    def withdraw(self, receipt_id: str) -> None: ...
```

域事件用 `dataclass`，Engine `emit` 在 commit 后广播给 Runtime（Runtime 决定哪些转 notice）：

```python
@dataclass(frozen=True)
class DomainEvent:
    name: str
    payload: dict

class DomainEvents:
    TRIGGER_RECEIVED = "trigger.received"
    EVAL_STARTED = "eval.started"
    DECISION_RECORDED = "decision.recorded"
    TICKET_CREATED = "ticket.created"
    TICKET_RESUMED = "ticket.resumed"
    TICKET_ENDED = "ticket.ended"
    ASSIGNMENT_STARTED = "assignment.started"
    ASSIGNMENT_ENDED = "assignment.ended"
    THREAD_SUBSCRIBED = "thread.subscribed"
    THREAD_UNSUBSCRIBED = "thread.unsubscribed"
    TRIGGER_REQUEUED = "trigger.requeued"
    POLICY_CHANGED = "policy.changed"
```

## 2. Engine 主体与角色 Pick

```python
# superagent/core/engine.py
class Engine:
    """唯一 writer of Triggers/Threads/Decisions/Tickets/Assignments。
    所有方法同步。每命令一事务。"""
    def __init__(self, opts: EngineOptions):
        self.repo = opts.repo
        self.policy_get = opts.policy_get        # () -> Policy
        self.scope = opts.scope                  # SourceScope
        self.assessor = opts.assessor
        self.executor = opts.executor
        self.outbox = opts.outbox
        self.prepare_ticket = opts.prepare_ticket
        self.now = opts.now or _iso_now
        self.emit = opts.emit or (lambda ev: None)
        # 五部件，单向依赖
        self.replies = Replies(self)
        self.tickets = Tickets(self)
        self.assignments = Assignments(self)
        self.evals = Evals(self)
        self.intake = Intake(self)

    # Sources 角色
    def receive(self, trigger: Trigger) -> str: ...
    def bind_thread(self, ticket_id, thread, own) -> None: ...
    def thread_closed(self, thread, outcome, at) -> None: ...
    # main Session 角色
    def candidate(self, trigger_id) -> Optional[dict]: ...
    def candidate_delivered(self, eval_id) -> None: ...
    def decide(self, trigger_id, outcome, reason=None) -> None: ...
    def attach(self, ticket_id, trigger_id, note=None) -> None: ...
    def request_status(self, ticket_id) -> bool: ...
    def note(self, ticket_id, text) -> None: ...
    def reply(self, request) -> None: ...
    # people 角色
    def stop_ticket(self, ticket_id, reason=None) -> None: ...
    def retry_ticket(self, ticket_id) -> None: ...
    def reevaluate(self, trigger_id) -> None: ...
    def unsubscribe(self, thread) -> None: ...
    def dismiss(self, decision_id) -> None: ...
    # executor 角色
    def assignment_started(self, ticket_id, assignment_id) -> None: ...
    def inputs_delivered(self, ticket_id, assignment_id, input_ids) -> None: ...
    def inputs_read(self, ticket_id, input_ids) -> None: ...
    def turn_ended(self, ticket_id, assignment_id) -> None: ...
    def delivery_failed(self, ticket_id, assignment_id, error) -> None: ...
    def report(self, ticket_id, report: Report) -> None: ...
    def mark_unrelated(self, ticket_id, thread, reason) -> None: ...
    def follow_for_context(self, ticket_id, thread) -> None: ...
    def handback(self, ticket_id, trigger_id, reason) -> bool: ...
    def assignment_interrupted(self, ticket_id, assignment_id) -> None: ...
    # passes 角色
    def evaluate_due(self) -> None: ...
    # settings 角色
    def policy_changed(self, old, now, effects) -> None: ...
```

角色接口用 `typing.Protocol` 单独定义（`EngineForSources`、`EngineForMainSession`、`EngineForPeople`、`EngineForSettings`、`EngineForExecutor`），每个是 `Engine` 的 `Pick`。Python 没有真 Pick，用 Protocol 列出允许的方法，构造时传 `cast(EngineForSources, engine)`。

## 3. 五部件（单向依赖 Intake → Evals → Tickets → Assignments → Replies）

依赖方向固定：Intake → Evals → Tickets → Assignments → Replies。跨部件命令（`attach`、`evaluate_due`、`policy_changed`）按序调各部件。

### Intake（`superagent/core/intake.py`）

`receive(trigger)`：

1. `repo.triggers.get(trigger.id)` —— 存在 → `duplicate`（幂等）。
2. gate 不在 Engine：Source 的 `scope()` 已在 SourceRegistry 过滤；Intake 收到的都是 listened。但仍记 `disposition.filtered`（Source 给的 reason）。
3. 存 Trigger + `trigger_thread` 关联 + Thread（若新建）。
4. 扇出：Thread 的所有 live Subscription（含 ended 未人 stop 的）收到 Input（fanout）；人消息到达 Ticket 时**不**进 Eval（归该 Ticket Executor）。
5. 除非人消息已到 Ticket，否则 Trigger 在 primary Thread 排队进 Eval。
6. `emit(TRIGGER_RECEIVED)`。

### Evals（`superagent/core/evals.py`）

`evaluate_due()`：除非 paused，对每个有 queued Triggers 的 Thread 起 Eval（一个 Thread 一次，batch = 期间到达的全部）；超时（candidate delivered 后起计，未 delivered 不超时）重排，两次后第三次记 `unavailable`。

`decide(trigger_id, outcome, reason=None)`：记 Decision。`accept` 创建 Ticket + Mandate + first Assignment + Subscriptions + ack；`stop` 撤 Mandate；`ignore` 仅记；`unavailable` 记 + 立即重排当前版本。

inferred accept 必须 assessment；card 明显无规则命中可直 ignore（`ignoredFromCard`）。Direct accept 拒非 explicit 的 Trigger。

### Tickets（`superagent/core/tickets.py`）

生命周期：

- **create**：accept 时，origin/mandate/first assignment/channels/subscriptions 一次事务写。ack 通过 Replies 发。
- **resume**：人消息在 ended Ticket 的 Thread 上 → 不新 Eval，原 Mandate revalidate（见 07-authorization）→ 通过则 reopen + 新 Input + 唤醒 Executor。
- **retry**：结束当前 Assignment + 起新 Assignment（同 Session/目录/branch/PR）。
- **stop**：人 stop → 撤 Mandate + Ticket 永久 ended；stop 消息若被 handback 可再 Eval。
- **end**：`ready` 无 PR → completed；`dropped` → dropped；跟随的 PR 全关闭 → end with outcome（merged/closed）。
- **end 时释放 follow-up**：未读/未 ack/未 handback 的 Input 回 Eval（若无其他 Ticket own）。

Thread 跟随规则：Ticket 跟随它开的 PR + 起源 PR；reopened PR 重跟；`bind_thread` 由 Source 调；context-only 跟随不授权不唤醒。

### Assignments（`superagent/core/assignments.py`）

记录 Executor 事实：

- `assignment_started`：Assignment 转 RUNNING，waiting Inputs 投出。
- `inputs_delivered` / `inputs_read`：Input 状态。`inputs_read` 一次（step 取入 history 后）；cancel/error 前的 step 不算 read。
- `turn_ended`：turn 结束 + 无后台作业 → 报 waiting；有则 keep slot。
- `delivery_failed`：记 delivery 错误，重试到成功/新 Assignment/Ticket end。
- `report`：记 Report + question；`dropped` end Ticket；`ready` 完成（无 Thread 跟随）或等 PR 关闭。
- `assignment_interrupted`：host 停时 running Assignment 重开带 recovery note；已 report 的保 Session。

### Replies（`superagent/core/replies.py`）

通过 `Outbox.send` 写 Channel 消息：

- `acknowledgement`：accept 时发，≤100 字。
- `answer`：Executor 答人 follow-up。
- `result`：ready 报告的 PR/结果通知。
- `attention`：forward Executor 的 question，不改写。

每条按规则允许；同 Ticket 同会话消息**有序**（前序未发/未失败 3 次则等）。

## 4. 事务模型

```python
def receive(self, trigger):
    with self.repo.atomic as tx:
        if self.repo.triggers.exists(trigger.id):
            return "duplicate"
        self.repo.triggers.save(trigger)
        self._fanout(trigger, tx)             # 写 Input
        self._queue_eval(trigger, tx)         # 排队
        tx.after_commit(lambda: self.emit(DomainEvent(...)))
        # 端口在事务内入队 effect（launch/delivery/relay/notice/reply）
        # 适配器在 commit 后执行（Effects handler）
    return "stored"
```

- `repo.atomic` 是 SQLite 事务（`BEGIN IMMEDIATE`）。
- `after_commit` 队列：commit 后逐个调；rollback 后丢弃。
- 端口（Executor.start、Outbox.send）在事务内 `enqueue` effect，Effects handler 在 commit 后执行——避免回滚后已发外部消息。

## 5. 一写者表（对应原型 007）

| State | Writer |
|---|---|
| Triggers / Threads / Decisions / Tickets / Assignments | Engine |
| Source 读位置 + Thread meta | 该 Source（经 SourceContext） |
| Settings + history | Settings |
| Prompt versions | Prompts |
| Effect rows | Effects |
| Outbox receipts | Outbox |
| Health entry | claim 它的部件 |

Source 改 Ticket **只能**经 `SourceContext.tickets.bind_thread` / `thread_closed`，调 Engine。

## 6. 错误（对应原型 `src/core/errors.py`）

```python
# superagent/core/errors.py
class UserError(Exception):
    def __init__(self, code: str, values: dict | None = None):
        self.code = code
        self.values = values or {}
        super().__init__(code)

USER_ERRORS: dict[str, str] = {
    "not_found": "...",
    "not_latest": "...",
    "followup_owned": "A Ticket still owns this follow-up; only stop is allowed.",
    "already_ended": "...",
    "cannot_retry": "...",
    "stale_revision": "...",
    # 完整列表见原型
}
```

HTTP 层：`missing` → 404；其余 `UserError` → 400 `{code, values}`；`stale_revision` → 409；其余 500。

## 7. 命令命名约定

- 请求某事：imperative + object（`stop_ticket`、`bind_thread`）——可拒绝。
- 记录适配器事实：过去时（`assignment_started`、`delivery_failed`）——不拒绝。

## 8. 与 Sherry 的耦合点（详见 12-sherry-integration.md）

- `Executor.start` → `runtime/hooks.py` 注册的 `spawn_subagent_direct`。
- `Executor.stop` → `turn_runner.cancel`。
- `turn_ended` / `inputs_read` → Sherry subagent 的完成/状态回调（经 hooks）。

Engine 本身**不 import** Sherry；它调 Executor port，Executor 实现里调 hooks。

## 9. 该册验收门

- `superagent/core/engine.py` + 五部件文件齐备，方法签名与 §2 一致。
- 角色接口 5 个 Protocol 定义在 `superagent/core/ports.py`。
- 事务模型：`receive` 在 duplicate 时返回 `"duplicate"` 不写；stored 时一个事务内写 trigger+fanout+queue。
- `after_commit` 单测：rollback 后回调不执行；commit 后执行。
- 一写者表 enforced by lint-imports（`superagent.sources` 不 import `superagent.app.settings` 等）。
- `UserError` + `USER_ERRORS` key 与原型 `src/core/errors.ts` 一致。
- 域事件 12 个常量齐备。
- `tests/superagent/core/test_engine.py` 镜像 `tests/core-engine.spec.ts`（见 13-test-plan.md）。
