# 09 Effects 与 Outbox：持久队列、收据、重试

复刻原型 `src/app/effects.ts` + `src/app/outbox.py` + `src/infra/health.py` + `003-execution.md §Stored work`。Effects 是 commit 后才执行的 durable side-effect 队列；Outbox 是 Channel 消息的收据单写者。

## 1. Effects（`superagent/app/effects.py`）

```python
class Later(Exception):
    """不计数失败、延后（如无 worker slot）。"""

class Effects:
    """durable side-effect 队列。一 (kind, key) 单行。"""
    def __init__(self, store: Store, board: HealthBoard):
        self.store = store
        self.board = board
        self._handlers: dict[str, EffectHandler] = {}
        self._concurrency: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def enqueue(self, kind: str, key: str, payload: dict) -> None:
        """Engine 在事务内调。同 waiting key → 替换 payload + 立即 due + 不变顺序。"""
        self.store.effects.upsert(kind, key, payload)

    def handle(self, kind: str, run: Callable, *, concurrency: int = 1,
               retry: tuple[int, int] = (2, 60)) -> None:
        """注册 handler。一 kind 一个。"""
        self._handlers[kind] = EffectHandler(run, concurrency, retry)

    async def start(self) -> None: ...   # setup 后开始
    async def drain(self) -> None: ...    # shutdown：停处理、保留 pending
    def pending(self) -> int: ...
    def idle(self) -> bool: ...
```

### 1.1 处理流程

```python
async def _loop(self):
    while not self._stop.is_set():
        await self._wake.wait(); self._wake.clear()
        for kind, handler in self._handlers.items():
            sem = asyncio.Semaphore(handler.concurrency)
            async def run_one(row):
                async with sem:
                    try:
                        await handler.run(row.payload)
                        self.store.effects.set_done(row.id)
                        self.board.clear(kind)
                    except Later:
                        self.store.effects.reschedule(row.id, delay=2)
                    except Exception as e:
                        attempts = self.store.effects.bump_attempts(row.id, str(e))
                        if attempts >= 3:
                            self.board.report(kind, {"state": "error", "detail": str(e)})
                        delay = _backoff(attempts, handler.retry)
                        self.store.effects.reschedule(row.id, delay=delay)
            due = self.store.effects.due_rows()
            await asyncio.gather(*[run_one(r) for r in due if r.kind == kind])
        await asyncio.sleep(2)  # Passes 周期
```

退避：`5 * 2 ** (attempts-1)` clamp 到 `[min, max]`（按 kind 配）。

### 1.2 六种 effect

| Kind | Handler | 干什么 | 并发 | 重试 |
|---|---|---|---|---|
| `assessment` | MasterAssessor | steer candidate card 进 main Session turn → Engine candidate_delivered → 起 Eval deadline | 4，paused 时 0 | 2s→60s |
| `launch` | SherryExecutor | 起 Assignment：prepare → subagent → assignment message | free worker slots，paused 时 0 | transient 5s→300s 三次；其他失败 fail Ticket；paused/full → 5s |
| `delivery` | SherryExecutor | 把 waiting Inputs 送给 running Assignment，有序 | 4，paused 时 0 | 2s→60s；等 slot → 2s |
| `relay` | Sessions | worker report/question 或人 follow-up 进 main Session turn | 1，按序 | 2s→60s |
| `notice` | Sessions | 注入 main Session 不唤醒 | 1，按序 | 2s→60s |
| `reply` | Outbox | 经 Source 发 Channel 消息（前序未发/未失败 3 次则等） | 1，paused 时 0 | 5s→300s；Source off/落后 → 5s |

每 kind 一个健康项，连续 3 次失败才报 error；`delivery` 失败显示在 Ticket 而非系统问题。

### 1.3 coalescing 与顺序

- 同 (kind, key) waiting → 替换 payload + 立即 due + **不变队列顺序**。
- relay/notice 保序：failed 的 holds back 后续同类。
- reply 保序：同 Ticket 同会话的前序未发/未失败 3 次则后序等。

## 2. Outbox（`superagent/app/outbox.py`）

```python
class Outbox:
    """Engine 的 Outbox port + 收据单写者。"""
    def __init__(self, store: Store, sources: SourceRegistry, board: HealthBoard):
        self.store = store
        self.sources = sources
        self.board = board

    def send(self, message: Receipt) -> None:
        """Engine 在事务内调：存收据 + 入 reply effect。"""
        self.store.receipts.add(message)   # idempotency: id 唯一
        enqueue("reply", message.id, payload={"receipt_id": message.id})

    def status(self, receipt_id: str) -> Optional[str]:
        return self.store.receipts.get(receipt_id).status

    def withdraw(self, receipt_id: str) -> None:
        """Source 不再听目的地 → dropped。"""
        self.store.receipts.set_dropped(receipt_id, reason="channelGone")
```

### 2.1 reply effect handler

```python
async def _handle_reply(self, payload):
    receipt = self.store.receipts.get(payload["receipt_id"])
    if receipt.status in ("sent", "dropped"):
        return
    # 前序检查：同 Ticket 同会话有未发且失败<3 的 → 等
    if self.store.receipts.behind(receipt):
        raise Later()
    # Source allowed?
    if not self.sources.channel_allowed(receipt.channel, config):
        self.withdraw(receipt.id)
        return
    try:
        result = self.sources.send(receipt)
        if result == "sent":
            self.store.receipts.set_sent(receipt.id)
            self.board.clear("reply")
        elif result == "gone":
            self.store.receipts.set_dropped(receipt.id, reason="channelGone")
    except Exception as e:
        # throw → 重试 5s→300s
        raise
```

### 2.2 投递三态

- `sent`：成功，收据 settled。
- `gone`：永久丢（对话消失/Source 不再听）→ 收据 `dropped`，不再排队。原因 `channelGone`（对话没了）或 `channelIgnored`（Source 不听了）。
- throw：重试 5s→300s。`CliError.code` 是 CLI 报的 code。

### 2.3 idempotency

- receipt id = message id（idempotency key）。
- Source 用 id 防重复：GitHub 隐藏 comment marker（`<!-- dsh-superagent:<id> -->`）；Feishu idempotency key；webhook `--key`。
- 重试可能在远端已收后失败，故必须幂等。

### 2.4 排序

- 同 Ticket 同会话消息按入队顺序发：ack 先于 answer/result。
- 前序失败 3 次才放行后序（避免一条卡死全部）。
- 其他 Ticket / 无 Ticket 消息不等。

### 2.5 dropped 消息

- 保留记录（when/why/Ticket），不再排队。
- dropped ack 读作 dropped（不是 sent）。
- 面板 Source 页显示：channel + preview + ticket + thread + dropped.at/reason。
- Source off 时消息 wait（不 drop）；Source 不再听目的地才 drop。

## 3. HealthBoard（`superagent/infra/health.py`）

```python
class HealthBoard:
    """每健康项一个 writer，claim 防碰撞。"""
    def claim(self, name: str, owner: str) -> None:
        """注册 owner。拒碰撞（含保留名 runtime/reply/worker/effect kind/sync:<id>）。"""
        ...
    def report(self, name: str, state: dict) -> None:
        """state: {state: starting|ready|paused|error, detail, at}"""
        ...
    def clear(self, name: str) -> None:
        """下次成功自清。"""
        ...
    def all(self) -> dict[str, dict]: ...
```

健康项分组（对应 006 §Health entries）：

| Entry | 报什么 |
|---|---|
| `runtime` | 应用自身 |
| `models` | 等 model-budget retry 的 Session |
| `evals` | 最近 1h 无后续 judgement/dismissal 的 unavailable Eval |
| `assessment`/`launch`/`delivery`/`relay`/`notice`/`reply` | Effects 各 kind |
| `worker` | Executor Session 事件处理（首次失败后出现） |
| `github`/`feishu`/`history` | Source 轮询/事件/历史恢复 |
| `sync:<source id>` | allowed-list 同步 |

**规则**：连续 3 次失败才报 error；下次成功自清。一次网络错不报。`worker` 首次失败后才出现（健康时不占位，符合面板"quiet by default"）。

## 4. Passes（`superagent/app/passes.py`）

```python
class Passes:
    """一次一个 pass，每 2 秒 + 命令触发。
    每 pass：先 due effects，再 Engine.evaluateDue。
    Source Trigger 经它到 Engine。"""
    def __init__(self, runtime, interval_s=2.0): ...
    async def start(self): ...
    async def stop(self): ...
    def request(self): self._wake.set()  # 立即下一 pass
```

pass 内：due effects（Effects handler 各并发）→ `Engine.evaluate_due()`（一个事务一个 Eval/Ticket）。

## 5. notices（`superagent/app/notices.py`）

```python
def event_notice(event: DomainEvent) -> Optional[Notice]:
    """domain event → main Session notice（不唤醒）。"""
    # ticket.ended / trigger.requeued / thread.unsubscribed → notice
    ...

def report_relay(report, ticket) -> Relay:
    """worker report → main Session。
    question 立即 forward；普通 progress 留 Ticket；其他 relay。"""
    ...

def followup_relay(trigger, tickets) -> Relay:
    """人 follow-up → main Session（命名 owning Tickets）。"""
    ...

def result_relay(...) -> Relay: ...
```

relay/notice 经 Sessions 投 main Session；turn 中到达的 relay 在下一 step 一起投（DeepSeek Harness inbox 行为 → Sherry state_port.add_messages）。

## 6. 该册验收门

- `Effects` 实现 enqueue/coalescing/顺序/退避/Later。
- 六种 effect handler 注册 + 各自并发/重试配置。
- `Outbox` send/status/withdraw；reply effect handler 三态（sent/gone/throw）。
- idempotency：重复 send 同 id 不重复发。
- 排序：同 Ticket 同会话保序；前序失败 3 次放行后序。
- dropped 消息保留 + 面板可见。
- `HealthBoard` claim 防碰撞 + 连续 3 次失败报 error + 自清。
- `Passes` 每 2 秒 + 命令触发；一次一 pass。
- `tests/superagent/test_effects.py` 镜像 `tests/effects.spec.ts`。
- `tests/superagent/test_outbox.py` 镜像 `tests/outbox.spec.ts`。
- `tests/superagent/test_passes.py` 镜像 `tests/passes.spec.ts`。
- `tests/superagent/test_health.py` 镜像 `tests/health.spec.ts`。
- `tests/superagent/test_queue.py` 镜像 `tests/queue.spec.ts`（Runtime 经 effect 队列的 work + 早期版本存储）。
