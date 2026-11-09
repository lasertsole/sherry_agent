# 04 Store：SQLite schema、Repository、KV、迁移

复刻原型 `src/app/store.ts` + `007-architecture.md §Persistence`。单 SQLite 文件 `<stateDir>/orchestrator.sqlite`，目录锁防第二进程。

## 1. 文件布局

```
<stateDir>/
├── orchestrator.sqlite      # 唯一数据库（Store 独占）
├── lock                     # 进程锁（flock）
├── worker-bin/              # 命令 shim（setup 写）
├── worker-gitconfig/        # git 配置（setup 写）
├── work/                    # Ticket 目录（默认执行根，可配）
└── control/                 # service 脚本用（stop marker/pid）
```

## 2. 表 schema

> **修正(问题 4)**:不用 SQLite JSON path 生成列(`$[#-1]` 算术不被支持,且序列化 shape 变化会让生成列静默失效)。改用**显式列**——Engine 写时维护,查询走普通索引。原型用生成列是因为 TS 侧 JSON 直接存;Python 侧用 dataclass,显式列更稳。

全部 SQL 写在 `superagent/app/store.py` 顶部常量 + `migration.py`。SQLite 用 `sqlite3`(stdlib)+ `WAL` 模式。**不**引入 ORM。

```sql
-- SCHEMA_VERSION = 11

CREATE TABLE trigger (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  actor_json TEXT NOT NULL,
  human INTEGER NOT NULL,
  addressed TEXT,
  at TEXT NOT NULL,
  text TEXT NOT NULL,
  channel_json TEXT,
  url TEXT,
  meta_json TEXT NOT NULL
);

CREATE TABLE disposition (
  trigger_id TEXT PRIMARY KEY REFERENCES trigger(id),
  filtered INTEGER NOT NULL,
  filter_reason TEXT,
  eval_id TEXT,
  decision_id TEXT,
  reached_json TEXT,
  handed_back_json TEXT,
  acknowledged_json TEXT,
  requeued INTEGER DEFAULT 0
);

CREATE TABLE trigger_thread (
  trigger_id TEXT NOT NULL REFERENCES trigger(id),
  thread_key TEXT NOT NULL,
  source TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  at TEXT NOT NULL,
  human INTEGER NOT NULL,
  PRIMARY KEY (trigger_id, thread_key)
);
CREATE INDEX ix_trigger_thread_thread ON trigger_thread(thread_key, at, trigger_id);

CREATE TABLE thread (
  thread_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  subscriptions_json TEXT NOT NULL,
  context_json TEXT,
  queued_trigger_ids_json TEXT,
  eval_id TEXT,
  expired_count INTEGER DEFAULT 0,
  meta_json TEXT,
  -- 显式列(Engine 写时维护)
  has_queued INTEGER NOT NULL DEFAULT 0,        -- queued_trigger_ids 非空时 1
  has_eval INTEGER NOT NULL DEFAULT 0,           -- eval_id 非空时 1
  is_active INTEGER GENERATED ALWAYS AS (has_queued OR has_eval) VIRTUAL,  -- 简单 OR,无 JSON path
  is_subscribed INTEGER NOT NULL DEFAULT 0      -- subscriptions 非空时 1
);
CREATE INDEX ix_thread_active ON thread(is_active) WHERE is_active = 1;
CREATE INDEX ix_thread_subscribed ON thread(is_subscribed) WHERE is_subscribed = 1;

CREATE TABLE ticket (
  id TEXT PRIMARY KEY,
  origin_json TEXT NOT NULL,
  mandate_json TEXT NOT NULL,
  execution_json TEXT NOT NULL,
  channels_json TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  follows_json TEXT NOT NULL,
  assignments_json TEXT NOT NULL,
  latest_report_json TEXT,
  ended_json TEXT,
  -- 显式列(Engine 写时维护)
  ended_at TEXT,                                 -- NULL = open
  is_open INTEGER GENERATED ALWAYS AS (CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) VIRTUAL,
  latest_assignment_status TEXT,                 -- pending|running|ended
  has_undelivered_input INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER GENERATED ALWAYS AS (
    CASE WHEN ended_at IS NULL AND (has_undelivered_input = 1 OR latest_assignment_status IN ('pending','running')) THEN 1 ELSE 0 END
  ) VIRTUAL
);
CREATE INDEX ix_ticket_open ON ticket(is_open) WHERE is_open = 1;
CREATE INDEX ix_ticket_active ON ticket(is_active) WHERE is_active = 1;

CREATE TABLE decision (
  id TEXT PRIMARY KEY,
  thread_key TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason_json TEXT NOT NULL,
  at TEXT NOT NULL,
  trigger_ids_json TEXT NOT NULL
);
CREATE INDEX ix_decision_thread ON decision(thread_key, at);

CREATE TABLE effect (
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  due_at TEXT,
  UNIQUE(kind, key)
);
CREATE INDEX ix_effect_due ON effect(due_at) WHERE status = 'pending';

CREATE TABLE outbox (
  id TEXT PRIMARY KEY,
  ticket_id TEXT,
  channel_json TEXT NOT NULL,
  purpose TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  dropped_at TEXT,
  dropped_reason TEXT,
  is_dropped INTEGER GENERATED ALWAYS AS (CASE WHEN status = 'dropped' THEN 1 ELSE 0 END) VIRTUAL
);
CREATE INDEX ix_outbox_ticket_conv ON outbox(ticket_id, channel_json);
CREATE INDEX ix_outbox_dropped ON outbox(is_dropped) WHERE is_dropped = 1;

CREATE TABLE kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
```

**保留的生成列**只做简单 `CASE WHEN` 或 `OR`,不碰 JSON path。复杂状态(`has_queued`/`has_undelivered_input`/`latest_assignment_status`)是**显式列**,Engine 在写 Ticket/Thread 时同步更新。`TicketRepo.save` 必须在事务内更新这些显式列(单测覆盖)。

## 3. Repository 接口

```python
# superagent/app/store.py
class Store:
    def __init__(self, path: str, lock_dir: str):
        self._conn = sqlite3.connect(path, isolation_level=None)  # 手动事务
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = _acquire_lock(lock_dir)
        self._migrate()
        self.triggers = TriggerRepo(self._conn)
        self.threads = ThreadRepo(self._conn)
        self.tickets = TicketRepo(self._conn)
        self.decisions = DecisionRepo(self._conn)
        self.read = StoreRead(self._conn)
        self.receipts = ReceiptRepo(self._conn)  # outbox

    @property
    def atomic(self) -> ContextManager["Tx"]: ...

    def close(self) -> None:
        self._conn.close()
        self._lock.release()
```

每个 Repo 提供 `get(id) -> Optional[T]`、`save(obj) -> None`（upsert）、list 查询。序列化用 `json.dumps(obj.__dict__)`（dataclass → dict）。

`atomic` 是上下文管理器：

```python
@contextmanager
def atomic(self):
    self._conn.execute("BEGIN IMMEDIATE")
    after = []
    try:
        yield Tx(self._conn, after)
        self._conn.execute("COMMIT")
        for fn in after: fn()
    except:
        self._conn.execute("ROLLBACK")
        raise
```

## 4. 读查询（`store_read.py`，永不写）

对应原型 `src/app/store-read.ts`：

- `thread_triggers(thread_key, cursor, limit)` —— 时间序 + id。
- `trigger_stats(source_id, since)` —— 24h 计数。
- `search(query)` —— Trigger 文本/id/Feishu msg id/stored link。
- `dropped_replies(source_id, since)` —— newest first。
- `tickets_open()` / `tickets_active()` —— 生成列索引扫。
- `subscribed_threads(source_id)`。
- `failing_replies()` / `dropped_since(since)`。

## 5. KV（`superagent/kv.py` 单一登记）

```python
# superagent/kv.py
from typing import TypeVar, Generic
T = TypeVar("T")

class KVEntry(Generic[T]):
    def __init__(self, family: str, key: str, type_: type[T]):
        self.family = family; self.key = key; self.type = type_

class KV:
    """每个 kv 记录的 owner/key/type 在此声明且仅在此。"""
    SCHEMA = KVEntry("schema", "schema-version", int)
    SETTINGS_REVISION = KVEntry("settings", "settings-revision", int)
    # settings-version:<n>, settings-block-revision:<block>
    PROMPT_REVISION = KVEntry("prompts", "prompt-revision", int)
    MASTER_ID = KVEntry("main", "master-id", str)
    # session-prompts:<sid>
    # source-labels:<source_id>
    # github-cursor:<n>, thread-*
    # webhook.cursor 等（Source 自有 key 用 family）
```

`tests/superagent/test_store.py` 断言 key 字面量只出现在 `kv.py` + `migration.py`（与原型 `tests/store.spec.ts` 一致）。

## 6. 迁移（`superagent/app/migration.py`）

- `SCHEMA_VERSION = 11`（与原型当前对齐；后续若改 schema 才递增）。
- 启动时 `PRAGMA user_version` 检查；等于 11 直接用；小于 11 从 10→11 升级（一次性，事务内）；大于 11 拒绝启动（"a database migrated by a newer version cannot be opened by an older one"）。
- schema 10→11：把每条存储记录改写为当前 shape。原型 `src/app/migration.ts` 是参考。
- **Python 版从零开始的话直接建 schema 11**，但保留 migration 文件以兼容从原型 SQLite 文件迁移（便于测试和生产数据继承）。

## 7. 内存版（`superagent/core/memory.py`）

对应原型 `src/core/memory.ts`，供域测试：

```python
class MemoryRepositories:
    """Store 的内存镜像，同序、同嵌套回滚。
    域测试用，无 I/O。"""
    def __init__(self):
        self.triggers = {}  # dict[trigger_id, Trigger]
        self.threads = {}
        # ...
    @property
    def atomic(self): return MemoryAtomic(self)  # 深拷贝回滚
```

`tests/superagent/core/test_engine.py` 用 `MemoryRepositories`，不碰 SQLite。

## 8. TextCache（`superagent/app/cache.py`）

```python
class TextCache:
    """有界 LRU。读缓存（prompt 预览、Source 原文页等）。重启丢。"""
    def __init__(self, max_chars: int): ...
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str) -> None: ...  # 超限 drop LRU
```

## 9. 不删除策略（与原型一致）

原型明确："Nothing deletes old records yet, so these grow without bound"。Python 版第一版**同样不删**——trigger/disposition/decision/ended Ticket/outbox receipts/settings revision/prompt version 都保留。P2 再加保留策略（见 14-non-goals.md）。

## 10. 与 Sherry SQLite 的关系

- superagent 用自己的 `orchestrator.sqlite`，**不**碰 Sherry 的 `sherry.db`。
- 两库独立；如需关联（Ticket ↔ Sherry session_id），用 id 引用，不做外键。
- WAL 模式两边都开，互不锁。

## 11. 该册验收门

- `superagent/app/store.py` 建全部 §2 表 + 生成列 + 索引；`PRAGMA user_version=11`。
- `Store` 单测：起临时目录 + db 文件，CRUD 一个 Trigger/Thread/Ticket/Decision/Effect/Outbox/KV。
- `atomic` 单测：commit 后 `after_commit` 执行；抛异常后 rollback 且 `after_commit` 不执行。
- `MemoryRepositories` 与 SQLite Store 行为一致（同序同嵌套回滚）—— `tests/superagent/core/test_engine_memory.py` 与 `test_engine_sqlite.py` 用相同用例。
- `KV` 字面量只在 `kv.py` + `migration.py`（grep 断言）。
- 生成列：`thread.active=1` 当且仅当 `queued_trigger_ids` 非空或 `eval_id` 非空。
- 目录锁：起第二进程开同 stateDir → 拒绝。
- schema 升级：用一个 schema 10 的 db（手工构造或从原型导）→ 启动后升到 11，数据 shape 正确。
