# 计划：基础设施——stamp `turn_id` 到 messages 表

- 建档日期：2026-10-21
- 状态：方案设计，未开工
- 来源：`TODO/计划_压缩输出per-turn_brief与turn_num索引.md` 调研发现——sherry 已有 per-conversation-turn uuid（`current_turn_id`，由 `turn_scope` 在 turn 开始时设置）但没 stamp 到 `messages` 表行上，导致 `turn_index` 功能需要 `split_into_turns` workaround 归并 per-persist-batch `turn_num` 到 conversation turn
- 严重性：中（不影响现有功能，但多个未来功能——turn_index 归并、trajectory↔messages join、message_search turn handle、崩溃恢复对齐——都受制于这个缺口）
- 前置关系：**本计划是 `计划_压缩输出per-turn_brief与turn_num索引.md` 的基础设施前置**——落地后该计划的方案 B（`split_into_turns` 归并）可简化为按 `turn_id` 直接分组

---

## 0. 问题

### 0.1 sherry 已有两套 turn 概念，但没关联

| 标识 | 类型 | 粒度 | 赋值时机 | 存在哪儿 |
|---|---|---|---|---|
| **`turn_id`** | uuid4 字符串 | per-conversation-turn（一个用户提交 → 模型跑完） | `turn_scope`（`server/service/trajectory_store.py:52`）在 turn 开始时设 `current_turn_id` contextvar | trajectory 库 `trajectory_events` 表的 `turn_id` 列 + loguru context + WS frame |
| **`turn_num`** | per-session 整数 | per-persist-batch（每次 `add_messages` 一个新号） | `_persist_batch`（`context_engine/store/core.py:429`）`get_max_turn_num + 1` | `messages` 表的 `turn_num` 列（NOT NULL + 有索引） |

**两者没有共享键**——`messages` 表没有 `turn_id` 列，`trajectory_events` 表没有 `turn_num` 列。无法 join。一个 conversation turn 跨多个 `turn_num`（`aafter_model` 每次 model call flush 一次，`awrap_tool_call` 每次工具返回 flush 一次，每次一个新 `turn_num`），但所有 `turn_num` 共享同一个 `turn_id`（uuid）——只是这个 `turn_id` 没 stamp 到 message 行上。

### 0.2 跨层 import 障碍

`current_turn_id` contextvar 定义在 `server/service/trajectory_store.py:46`。`_persist_batch` 在 `context_engine/store/core.py`。`context_engine/` 不能 import `server/`（import-linter 契约约束，同 `agent/` 的约束）。需要走 `runtime/` 缝隙。

`runtime/` 是 AGENTS.md 明确设计的依赖倒置层：
- `runtime/hooks.py`："Process-level callback registry — agent/skills resolve server-owned callables without importing server"
- `runtime/data_provider.py`："workspace/context_engine obtain agent-owned data without importing agent"

已有先例：`RECORD_TRAJECTORY_EVENT`（`runtime/hooks.py:92`）让 `agent/trajectory_events.py:19` 调 server 的 `record_event` 而不 import `server/`。本计划用同一模式。

### 0.3 跨项目对照

| 项目 | turn 标识 | 粒度 | 在 message 行上？ |
|---|---|---|---|
| **sherry（现状）** | `turn_num`（整数）+ `turn_id`（uuid） | `turn_num` per-batch；`turn_id` per-conv-turn | `turn_num` 在；`turn_id` **不在** |
| codex | `turn_id` = `sub_id`（UUIDv7） | per-conv-turn | 不在 message 行上（在 event 流） |
| claudecode | `invocation_id`（UUID） | per-conv-turn | 不在 message 行上（在 event 流） |
| hermes-agent | `turn_id` = `session:task:hex8` | per-conv-turn | **不在任何表**（纯 runtime） |
| oh-my-openagent | `turn_id`（senpi 产出） | per-conv-turn | 不在 message 行上（senpi 内存） |

sherry stamp `turn_id` 到 `messages` 表后，是**唯一一个把 per-conversation-turn uuid 直接持久化到 message 行上的**——比同级项目更可查询。

### 0.4 一次 stamp，多个功能受益

| 功能 | 现在（无 turn_id on messages） | stamp 后 |
|---|---|---|
| `turn_index` 归并（`计划_压缩输出per-turn_brief与turn_num索引.md`） | `split_into_turns` workaround 归并 per-turn_num brief 到 conversation turn | 按 `turn_id` 直接分组，去掉 `split_into_turns` |
| trajectory ledger ↔ messages join | 无法 join——`trajectory_events.turn_id` 和 `messages.turn_num` 没共享键 | `messages.turn_id` = `trajectory_events.turn_id` 直接 join |
| `message_search` 返回 turn handle | 返回 `turn_num`（per-session，需复合键 `(session_id, turn_num)`） | 返回 `turn_id`（全局唯一 uuid，单键） |
| `get_turn_messages` | 查 `turn_summaries` 拿 `turn_num_end` 范围 → BETWEEN fetch | `WHERE turn_id = ?` 直接 fetch |
| 崩溃恢复后 turn 对齐 | `turn_num` 是 per-batch，崩溃后可能跟 `turn_id` 错位 | `turn_id` 跟着 message 行走，重启后仍对齐 |
| 调试 / 日志关联 | log 有 `turn_id`（loguru context）但 message 行没有，grep 跨表 | message 行有 `turn_id`，grep 一条命令跨表关联 |

---

## 1. 设计

### 1.1 `runtime/hooks.py` 加 `GET_CURRENT_TURN_ID` 缝隙

**文件**：`runtime/hooks.py`

跟 `RECORD_TRAJECTORY_EVENT`（`:92`）同模式——agent/context_engine 调 `resolve` 拿 server-owned 值，不 import `server/`：

```python
# runtime/hooks.py

GET_CURRENT_TURN_ID = "get_current_turn_id"
"""Hook: resolve the current conversation-turn uuid (current_turn_id contextvar).
Registered by server at assembly time; returns "" when no turn is active
(bare unit test, eval run, or pre-turn-scope code path)."""

# resolve 用现有的 register/resolve 机制（hooks.py:104-111）
# caller:
#   turn_id = hooks.resolve(GET_CURRENT_TURN_ID) or ""
```

**caller 兼容性**：`hooks.resolve` 返回 `None` 时 caller fallback `""`——跟 `RECORD_TRAJECTORY_EVENT` 的 "missing hook → silent drop" 同语义。在没组装过 server 的进程里（裸单测、eval）resolve 返回 `None`，`turn_id = ""`，不崩。

### 1.2 server 组装时注册 resolver

**文件**：`server/service/trajectory_store.py`（`install_trajectory_hooks` 旁边）

```python
# trajectory_store.py

def install_turn_id_hook() -> None:
    """注册 GET_CURRENT_TURN_ID resolver: 返回 current_turn_id contextvar 的值。"""
    from runtime import hooks

    def _get_current_turn_id() -> str:
        return current_turn_id.get()  # contextvar，turn_scope 设的值

    hooks.register(hooks.GET_CURRENT_TURN_ID, _get_current_turn_id)
```

**接入点**：`server/trigger/http/__init__.py:40-42` 现有 `install_trajectory_hooks()` 旁边加 `install_turn_id_hook()`——跟 trajectory hooks 同一批注册。

**contextvar 传播**：`_persist_batch` 在 `asyncio.to_thread`（`core.py:547`）里跑。Python 3.9+ 的 `asyncio.to_thread` **复制当前 context**（`contextvars.copy_context()`），所以 `current_turn_id` 从主线程传播到 worker 线程。`hooks.resolve(GET_CURRENT_TURN_ID)` 在 worker 线程里调 `_get_current_turn_id` → `current_turn_id.get()` 返回主线程 `turn_scope` 设的值。**contextvar 跨线程传播已验证可用**（`RECORD_TRAJECTORY_EVENT` 的 `record_event` 也在 `to_thread` 路径里读 `current_turn_id.get()`，`trajectory_store.py:329`）。

### 1.3 `messages` 表加 `turn_id` 列

**文件**：`context_engine/store/db.py`

**schema DDL**（`build_schema_v1`，`:182-216` 附近）：

```sql
-- 在现有 messages 表 CREATE 语句里加一列
turn_id TEXT,  -- per-conversation-turn uuid（turn_scope 赋值），NULL = 旧消息（stamp 前）
```

**自愈迁移**（`_HEAL_MESSAGE_COLUMN_DDL`，`:364-387` 附近）：

```python
# 跟 _DEADLINE_COLUMN_DDL / _SESSION_ID_COLUMN_DDL 同模式
_TURN_ID_COLUMN_DDL = f"ALTER TABLE {MESSAGES_TABLE_NAME} ADD COLUMN turn_id TEXT"
```

`_self_heal_schema`（`:479`）每次连接跑，已有列时 `ALTER TABLE ADD COLUMN` 抛 OperationalError 静默跳过（同现有模式）。旧 db 首次连接自动加列，旧行 `turn_id = NULL`。

**索引**：

```sql
CREATE INDEX IF NOT EXISTS idx_messages_turn_id ON messages(session_id, turn_id) WHERE turn_id IS NOT NULL
```

partial index——跳过 `NULL`（旧行）和 `""`（无 turn 活跃时的 persist）。`WHERE turn_id IS NOT NULL` 让索引不膨胀。

**不加 UNIQUE**——同一 `turn_id` 对应多个 message 行（一个 conversation turn 跨多个 persist batch），多行共享一个 `turn_id` 是正常态。

### 1.4 `_persist_batch` stamp `turn_id` 到行 + BaseMessage

**文件**：`context_engine/store/core.py::_persist_batch`（`:415-519`）

两处加 `turn_id`：

**a) INSERT 行里加 `turn_id` 列**——`_persist_batch` 构建 row dict 时读 `turn_id`：

```python
# _persist_batch 内，在 current_turn 赋值后（:429 附近）
from runtime import hooks
turn_id = hooks.resolve(hooks.GET_CURRENT_TURN_ID) or ""

# row 构建循环里（跟 row["turn_num"] = current_turn 同位置）
for index, (message, row) in enumerate(paired):
    row["turn_num"] = current_turn
    row["turn_id"] = turn_id          # ← 加这一行
    row["timestamp"] = base_timestamp
    ...
```

INSERT SQL 的列列表和占位符也要加 `turn_id`。现有 INSERT（`:497-510`）是 `INSERT OR IGNORE INTO messages (session_id, turn_num, role, ...)`，加 `turn_id` 到列列表 + 占位符。

**b) BaseMessage stamp**——末尾循环（`:515-519`）加一行：

```python
# 现有循环
for message in pending:
    message.additional_kwargs[_DB_PERSISTED_KEY] = True
    message.additional_kwargs["turn_num"] = current_turn    # turn_index 计划 §1.1
    message.additional_kwargs["turn_id"] = turn_id         # ← 本计划加
```

stamp 到 `additional_kwargs` 让压缩路径从 `messages_to_summarize` 直接读 `msg.additional_kwargs["turn_id"]`——不需要查 DB。

**hook resolve 失败时**：`turn_id = ""`（空字符串），INSERT 写空字符串，BaseMessage stamp 空字符串。下游 `_validate_and_consume_turn_index` 看到 `turn_id == ""` 跳过（同 `turn_num is None` 的降级逻辑）。

### 1.5 `turn_index` 计划的简化

本计划落地后，`计划_压缩输出per-turn_brief与turn_num索引.md` 的以下部分可简化：

| 章节 | 简化前（方案 B，`split_into_turns`） | 简化后（按 `turn_id` 分组） |
|---|---|---|
| §1.1 stamp | stamp `turn_num`（per-batch 整数） | stamp `turn_num` + `turn_id`（per-conv-turn uuid）——`_persist_batch` 一行加 |
| §1.3.4 `_validate_and_consume_turn_index` | `split_into_turns(messages)` 切 conv turn 边界 → 映射 turn_num → conv_idx → 归并 | **去掉 `split_into_turns`**——直接按 `msg.additional_kwargs["turn_id"]` 分组 |
| §1.4 `turn_summaries` schema | `turn_num`（MIN）+ `turn_num_end`（MAX）做范围 | `turn_id TEXT` 做索引键（一个 uuid 一行） |
| §1.5 `upsert_turn_summaries` | 接受 `turn_num_ends: dict[int, int]` 参数 | 接受 `turn_id` 字段，不需要 `turn_num_ends` |
| §1.8 `get_turn_messages` | 查 `turn_summaries` 拿 `[turn_num, turn_num_end]` 范围 → BETWEEN fetch | `WHERE turn_id = ?` 直接 fetch |
| §2 文件清单 | `summary_doc.py` + `summary_generation.py` + `compression.py` | 同 + `db.py` + `runtime/hooks.py` + `trajectory_store.py` + `store/core.py` |
| §3 测试 | `split_into_turns` 归并测试 + `turn_num_end` 范围测试 | 按 `turn_id` 分组测试 + `WHERE turn_id = ?` fetch 测试 |

**简化后净减代码量**：去掉 `split_into_turns` 归并逻辑（~30 行）+ `turn_num_end` 列 + 范围查询（~15 行）= ~45 行；本计划新增 ~35 行基础设施。净 +(-10) 行——基本持平，但**架构更干净**（无 workaround，跟同级项目对齐）。

### 1.6 `idempotency_key` 是否需要改

现有 `idempotency_key` = `f"{session_id}:{current_turn}:{turn_ms}:{index}:{digest}"`（`core.py:432-436`）。它编码 `turn_num`（`current_turn`）但不编码 `turn_id`。

**不改**——`idempotency_key` 的目的是 dedup（同一消息重复 persist 时跳过），`turn_num` + `turn_ms` + `index` + `digest` 已足够唯一。加 `turn_id` 不增加 dedup 能力（同一 turn 的 `turn_id` 相同，不影响唯一性）。

---

## 2. 关键文件清单

| 文件 | 改动 |
|---|---|
| `runtime/hooks.py` | 加 `GET_CURRENT_TURN_ID` 常量（~3 行） |
| `server/service/trajectory_store.py` | 加 `install_turn_id_hook()` 注册 resolver（~8 行） |
| `server/trigger/http/__init__.py` | 调 `install_turn_id_hook()`（~1 行，在 `install_trajectory_hooks()` 旁边） |
| `context_engine/store/db.py` | `build_schema_v1` 的 `messages` CREATE 加 `turn_id TEXT` 列；`_HEAL_MESSAGE_COLUMN_DDL` 加 `_TURN_ID_COLUMN_DDL`；加 `idx_messages_turn_id` partial index |
| `context_engine/store/core.py` | `_persist_batch`：读 hook 拿 `turn_id` + row dict 加 `turn_id` + INSERT SQL 加列 + BaseMessage stamp 加一行 |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| `RECORD_TRAJECTORY_EVENT` hook 模式 | `runtime/hooks.py:92` + `trajectory_store.py:341-345` | `GET_CURRENT_TURN_ID` 镜像同模式 |
| `install_trajectory_hooks` 注册模式 | `server/trigger/http/__init__.py:40-42` | `install_turn_id_hook` 在同位置调 |
| `ALTER TABLE ADD COLUMN` 自愈模式 | `db.py:107, 113`（`_DEADLINE_COLUMN_DDL` / `_SESSION_ID_COLUMN_DDL`） | `_TURN_ID_COLUMN_DDL` 同模式 |
| `_DB_PERSISTED_KEY` stamp 循环 | `core.py:515-519` | 在同一循环里加 `turn_id` stamp |
| `current_turn_id` contextvar | `trajectory_store.py:46` | resolver 直接 `.get()` 读它 |
| `asyncio.to_thread` contextvar 传播 | Python 3.9+ `contextvars.copy_context()` | `to_thread` 自动复制 context，worker 线程能看到主线程设的 contextvar |

### 不引入的依赖

- 不引入新表（`messages` 表加列就够）
- 不引入新 contextvar（复用现有 `current_turn_id`）
- 不引入新 middleware（`MessagePersistenceMiddleware` 无改动——stamp 在 `_persist_batch` 内完成）
- 不引入跨 db 事务（`turn_id` 在 `messages` 表同一 db 文件内）

---

## 3. 测试

| 测试 | 场景 |
|---|---|
| `test_get_current_turn_id_hook_registered` | server 组装后 `hooks.resolve(GET_CURRENT_TURN_ID)` 返回 callable |
| `test_get_current_turn_id_returns_uuid` | `turn_scope("abc123", "sid")` 内调 `hooks.resolve(GET_CURRENT_TURN_ID)` → 返回 `"abc123"` |
| `test_get_current_turn_id_empty_outside_scope` | 不在 `turn_scope` 内调 → 返回 `None` 或 `""`（无活跃 turn） |
| `test_get_current_turn_id_in_to_thread` | `asyncio.to_thread` 里调 `hooks.resolve(GET_CURRENT_TURN_ID)` → 返回主线程 `turn_scope` 设的值（contextvar 跨线程传播） |
| `test_persist_batch_stamps_turn_id` | `turn_scope("abc123", "sid")` 内 persist 消息 → `messages` 表行的 `turn_id` == `"abc123"` |
| `test_persist_batch_turn_id_empty_without_hook` | 未注册 hook 时 persist → `turn_id` == `""`（不崩，降级） |
| `test_persist_batch_turn_id_empty_without_scope` | 注册了 hook 但不在 `turn_scope` 内 persist → `turn_id` == `""` |
| `test_persist_batch_turn_id_same_across_batches` | 一个 `turn_scope` 内 3 次 `add_messages`（model call + tool call + model call）→ 3 个不同 `turn_num` 但同一个 `turn_id` |
| `test_persist_batch_turn_id_different_across_scopes` | 两个 `turn_scope` 各 persist → 两个不同 `turn_id` |
| `test_message_additional_kwargs_has_turn_id` | persist 后 BaseMessage 的 `additional_kwargs["turn_id"]` == DB 行的 `turn_id` |
| `test_schema_migration_adds_turn_id_column` | 旧 db（无 `turn_id` 列）首次连接 → `_HEAL_MESSAGE_COLUMN_DDL` 自愈加列，不崩 |
| `test_idx_messages_turn_id_partial_index` | 有 `turn_id` 的行在索引里；`turn_id IS NULL` 的旧行不在索引里 |
| `test_old_messages_turn_id_null` | stamp 前的旧消息行 `turn_id IS NULL`（不回填——NULL 表示"stamp 前的消息"） |
| `test_turn_id_not_unique` | 同一 `turn_id` 对应多个 message 行（一个 conversation turn 跨多个 persist batch）——不报 UNIQUE 约束错误 |

---

## 4. 优先级与分阶段落地

**本计划是 `turn_index` 功能的基础设施前置**。落地顺序：

1. **P0（本计划）**：`runtime/hooks` 缝隙 + schema 加列 + `_persist_batch` stamp——~35 行基础设施
2. **P1（`turn_index` 功能）**：`计划_压缩输出per-turn_brief与turn_num索引.md` 简化版——按 `turn_id` 分组，不需要 `split_into_turns`

P0 落地后 P1 净减 ~45 行功能代码（去掉 `split_into_turns` 归并 + `turn_num_end` 范围查询）。总账 P0+P1 ≈ 方案 B 的代码量，但架构更干净。

**如果 P0 不先落地**：P1 走方案 B（`split_into_turns` 归并）也能工作，后续 P0 落地后 P1 再简化。

---

## 5. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| `messages` 表加列影响性能 | `ALTER TABLE ADD COLUMN` 在 SQLite 是元数据操作不复制数据；`turn_id TEXT` 列 nullable 不影响现有查询；partial index 只索引非 NULL 行 |
| `runtime/hooks` resolve 在 worker 线程里返回 None | `to_thread` 复制 context（Python 3.9+），contextvar 跨线程传播已验证（`RECORD_TRAJECTORY_EVENT` 同路径）；fallback `""` 不崩 |
| import-linter 契约阻止 `context_engine` import `runtime/hooks` | `runtime/` 是 AGENTS.md 明确的依赖倒置层；`context_engine` 已被 AGENTS.md 提到可 import `runtime/data_provider`；`runtime/hooks.py` 是 stdlib-only 叶子模块，任何层都能 import；落地前跑 `lint-imports` 验证 |
| hook 未注册时（裸单测、eval）stamp `turn_id = ""` | 下游（`_validate_and_consume_turn_index` 等）看到 `turn_id == ""` 跳过——同 `turn_num is None` 的降级逻辑 |
| `current_turn_id` contextvar 在 subagent 路径未设 | subagent spawn（`spawn/core.py:1128-1132`）只用 `logger.contextualize` 不调 `turn_scope`——subagent 的 `current_turn_id` 继承父 turn 的值（contextvar 在 subagent coroutine 里继承）。stamp 的 `turn_id` 是父 turn 的——语义正确（子代理的消息属于父 turn） |
| 旧行 `turn_id IS NULL` 影响查询 | 新查询用 `WHERE turn_id = ?`（非 NULL 值）自然跳过旧行；需要扫旧行时用 `turn_id IS NULL` 显式查 |

---

## 6. 已知不在本计划范围内

- **`turn_id` 回填旧行**：旧行 `turn_id = NULL`，不回填——NULL 表示"stamp 前的消息"，语义清晰。如果需要回填，可以用 `trajectory_events` 表的 `turn_id` + 时间戳关联（positional/heuristic join），但这是另一个独立任务。
- **`turn_num` 废弃**：`turn_num` 保留——MesMemory 的索引（`idx_messages_turn_num`）、`mark_messages_compacted` 按范围、`get_turns_by_turn_num_scope` 都依赖它。`turn_id` 是**补充**不是**替代**。
- **`idempotency_key` 改造**：不加 `turn_id` 到 `idempotency_key`——dedup 不需要它（`turn_num` + `turn_ms` + `index` + `digest` 已足够唯一）。
- **`trajectory_events` 表加 `turn_num`**：不反向关联——trajectory 库和 messages 库是独立子系统，`turn_id` 是它们之间的桥但不要求两边都有 `turn_num`。
- **subagent 的 `turn_scope` 修正**：subagent spawn 不调 `turn_scope`（只用 `logger.contextualize`）——stamp 的 `turn_id` 继承父值，语义正确。如果要给子代理独立 `turn_id`，是另一个任务（改 `spawn/core.py:1128-1132` 加 `turn_scope`，但那会影响 trajectory 库的事件归属）。

---

## 7. 一句话

sherry 已有 per-conversation-turn uuid（`current_turn_id`，由 `turn_scope` 在 turn 开始时设），但没 stamp 到 `messages` 表行上——`_persist_batch` 只存 per-persist-batch `turn_num`。本计划通过 `runtime/hooks` 缝隙（跟 `RECORD_TRAJECTORY_EVENT` 同模式，~35 行）让 `_persist_batch` 读 `current_turn_id` 并 stamp 到 message 行 + `additional_kwargs`，加 `turn_id TEXT` 列到 `messages` 表（自愈迁移）。落地后 `turn_index` 功能按 `turn_id` 分组不需要 `split_into_turns`，trajectory↔messages 可 join，`message_search` 可返回全局唯一 turn handle，`get_turn_messages` 不需要范围查询。是 `turn_index` 功能的前置基础设施——落地后该计划净减 ~45 行 workaround 代码。
