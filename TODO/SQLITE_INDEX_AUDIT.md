# SQLite 索引审计报告

**创建日期**：2026-09-15
**状态**：待评审

## 审计范围

全项目所有 SQLite 表（7 个 DB 文件、15 张表），逐表对比已有索引与实际查询模式，识别"该建索引而未建"的列。

## 总览

| 严重度         | 数量 | 说明                                                       |
| -------------- | ---- | ---------------------------------------------------------- |
| 高（应修复）   | 2    | taskflow 表 `status` / `deadline_ts`                       |
| 低（可选改进） | 3    | todolist `flow_id`、user_input_queue 复合、messages 布尔列 |
| 无需修复       | 10   | 索引已完备                                                 |

---

## 高优先级 — 需要修复

### 1. taskflow 表 — `status` 列无索引

**表定义**：`agent/tools/taskflow/registry/store_sqlite.py:59`

```sql
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    flow_id TEXT PRIMARY KEY,
    ...
    status TEXT NOT NULL,
    child_session_key TEXT,
    ...
    deadline_ts REAL
);
```

**已有索引**：仅 `flow_id` PRIMARY KEY。

**无索引的查询**：

| 文件              | 行号 | SQL 模式                                                 |
| ----------------- | ---- | -------------------------------------------------------- |
| `store_sqlite.py` | 510  | `WHERE status = ?`                                       |
| `store_sqlite.py` | 553  | `WHERE status IN (?, ?) ORDER BY expected_revision DESC` |
| `store_sqlite.py` | 592  | `WHERE status = ? ORDER BY expected_revision DESC`       |

**影响**：taskflow 表随使用积累大量 flow 记录，按状态筛选（running / completed / failed 等）每次全表扫描。

**建议**：

```sql
CREATE INDEX IF NOT EXISTS idx_taskflow_status ON {TABLE_NAME}(status);
```

### 2. taskflow 表 — `deadline_ts` 列无索引

**无索引的查询**：

| 文件              | 行号 | SQL 模式                                            |
| ----------------- | ---- | --------------------------------------------------- |
| `store_sqlite.py` | 497  | `WHERE deadline_ts IS NOT NULL AND deadline_ts < ?` |

**影响**：deadline 监控是周期性扫描（sweeper），每次全表扫描所有 flow 检查是否过期。

**建议**：

```sql
CREATE INDEX IF NOT EXISTS idx_taskflow_deadline ON {TABLE_NAME}(deadline_ts)
    WHERE deadline_ts IS NOT NULL;
```

### 落地方式

在 `agent/tools/taskflow/registry/store_sqlite.py` 的 `_CREATE_TABLE_SQL` 之后、`ensure_db` 初始化流程中追加上述两条 DDL。`CREATE INDEX IF NOT EXISTS` 幻等，对已有数据库安全。

---

## 低优先级 — 可选改进

### 3. todolist 表 — `flow_id` 列无索引

**表定义**：`agent/tools/todolist/registry/store_sqlite.py:70`

```sql
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    ...
    PRIMARY KEY (session_id, position)
);
```

**无索引的查询**：

| 文件              | 行号 | SQL 模式                                                 |
| ----------------- | ---- | -------------------------------------------------------- |
| `store_sqlite.py` | 304  | `WHERE session_id = ? AND flow_id = ? ORDER BY position` |

**分析**：PK `(session_id, position)` 覆盖 `session_id` 前缀，但 `flow_id` 在 session 内需逐行扫描。当单 session 的 todo 数量少时无影响；todo 多 + flow 多时有效。

**建议**（可选）：

```sql
CREATE INDEX IF NOT EXISTS idx_todolist_session_flow ON {TABLE_NAME}(session_id, flow_id);
```

### 4. user_input_queue — `session_id` 单列索引可升级为复合

**表定义**：`server/queue/user_input_queue.py:112`

**已有索引**：

```sql
idx_user_input_queue_session        ON (session_id)
uq_user_input_queue_client_msg_active ON (client_msg_id) WHERE ...
```

**无索引覆盖的查询**：

| 文件                  | 行号 | SQL 模式                                                                                        |
| --------------------- | ---- | ----------------------------------------------------------------------------------------------- |
| `user_input_queue.py` | 149  | `WHERE session_id = ? AND status = 'QUEUED' AND expires_at > ? ORDER BY created_at ASC, id ASC` |
| `user_input_queue.py` | 164  | 同上（批量 claim）                                                                              |
| `user_input_queue.py` | 183  | `WHERE session_id = ? AND status IN (...) AND expires_at <= ?`                                  |
| `user_input_queue.py` | 562  | `WHERE session_id = ? AND status IN (...) ORDER BY created_at ASC, id ASC`                      |

**分析**：现有 `idx_user_input_queue_session` 只覆盖 `session_id`，`status` 和 `expires_at` 需逐行过滤。但 `MAX_ACTIVE_PER_SESSION` 限制了单 session 队列深度，实际扫描行数少，收益有限。

**建议**（可选）：

```sql
CREATE INDEX IF NOT EXISTS idx_user_input_queue_session_status
    ON user_input_queue(session_id, status);
```

### 5. messages 表 — `compacted` / `context_eligible` 过滤列未索引

**已有索引**：

```sql
idx_messages_timestamp       ON (session_id, timestamp)
idx_messages_turn_num       ON (session_id, turn_num)
idx_messages_session_role   ON (session_id, role)
idx_messages_parent         ON (session_id, parent_message_id)
idx_messages_idempotency    UNIQUE ON (idempotency_key) WHERE ...
```

**无索引覆盖的查询**：

| 文件                           | 行号 | SQL 模式                                                                                              |
| ------------------------------ | ---- | ----------------------------------------------------------------------------------------------------- |
| `context_engine/store/core.py` | 586  | `WHERE session_id = ? AND turn_num >= ? AND turn_num <= ? AND compacted = 0 AND context_eligible = 1` |
| `context_engine/store/core.py` | 647  | 同上                                                                                                  |
| `context_engine/store/core.py` | 492  | `UPDATE ... WHERE session_id = ? AND turn_num <= ?` (+ compacted filter)                              |

**分析**：现有 `idx_messages_turn_num` 已将扫描收窄到目标 turn 范围，`compacted`/`context_eligible` 是低基数布尔列（0/1），在已收窄的结果集上额外过滤成本极低。SQLite 查询规划器对低基数列的独立索引利用率也不高。**收益微乎其微，不建议修改**。

---

## 无需修复 — 索引已完备

### context_engine/store/db.py — messages 表

| 索引                                                  | 覆盖查询                      |
| ----------------------------------------------------- | ----------------------------- |
| `idx_messages_timestamp (session_id, timestamp)`      | session + 时间范围            |
| `idx_messages_turn_num (session_id, turn_num)`        | session + turn 范围（最高频） |
| `idx_messages_session_role (session_id, role)`        | session + role 筛选           |
| `idx_messages_parent (session_id, parent_message_id)` | 消息树遍历                    |
| `idx_messages_idempotency UNIQUE (idempotency_key)`   | 幂等去重                      |
| `PRIMARY KEY (id)`                                    | 按 id 查找                    |

### context_engine/store/db.py — message_embeddings 表

| 索引                                  | 覆盖查询                   |
| ------------------------------------- | -------------------------- |
| `idx_embeddings_session (session_id)` | session 级 embedding 加载  |
| `UNIQUE (message_id)`                 | LEFT JOIN 去重 + JOIN 查找 |

### context_engine/store/db.py — events 表

| 索引                                   | 覆盖查询                      |
| -------------------------------------- | ----------------------------- |
| `UNIQUE (session_id, seq)`             | seq 唯一性 + session 内顺序查 |
| `idx_events_session (session_id, seq)` | session 事件回放              |

### context_engine/store/db.py — 其余辅助表

| 表                     | 索引                                                              | 状态    |
| ---------------------- | ----------------------------------------------------------------- | ------- |
| compression_locks      | `session_id PK`                                                   | ✅ 够用 |
| context_epoch          | `session_id PK`                                                   | ✅ 够用 |
| session_leafs          | `session_id PK`                                                   | ✅ 够用 |
| compaction_checkpoints | `PK (id)` + `idx_compaction_session (session_id, checkpoint_seq)` | ✅ 够用 |

### agent/tools/subagent/registry/ — subagent_runs / pending_injections

| 表                 | 索引                                                                 | 状态                                   |
| ------------------ | -------------------------------------------------------------------- | -------------------------------------- |
| subagent_runs      | `run_id PK`                                                          | ✅ 够用（KV 结构，全量扫描或 PK 查找） |
| pending_injections | `run_id PK` + `idx_pending_injections_status WHERE status='pending'` | ✅ 够用                                |

### runtime/session/state_register.py — states / context_epoch

| 表            | 索引                   | 状态    |
| ------------- | ---------------------- | ------- |
| states        | `PK (session_id, key)` | ✅ 够用 |
| context_epoch | `session_id PK`        | ✅ 够用 |

### server/queue/user_input_queue.py — user_input_queue

| 索引                                           | 状态              |
| ---------------------------------------------- | ----------------- |
| `id PK`                                        | ✅                |
| `idx_user_input_queue_session (session_id)`    | ✅ 覆盖主查询路径 |
| `uq_user_input_queue_client_msg_active UNIQUE` | ✅ 幂等去重       |

---

## 落地计划

| 优先级     | 改动                                  | 文件                                            | 风险           |
| ---------- | ------------------------------------- | ----------------------------------------------- | -------------- |
| **P0**     | `idx_taskflow_status`                 | `agent/tools/taskflow/registry/store_sqlite.py` | 低（幻等 DDL） |
| **P0**     | `idx_taskflow_deadline`               | 同上                                            | 低             |
| P2（可选） | `idx_todolist_session_flow`           | `agent/tools/todolist/registry/store_sqlite.py` | 低             |
| P2（可选） | `idx_user_input_queue_session_status` | `server/queue/user_input_queue.py`              | 低             |

P0 两条 DDL 可在 `ensure_db` 初始化流程中追加，`CREATE INDEX IF NOT EXISTS` 对已有数据库安全，无需迁移脚本。
