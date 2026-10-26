# 方案:证据台账加 flow_id 列(TaskFlow 路径重构)

## 设计目标

给 `evidence_events` 表加 `flow_id` 列 + 索引,让 `taskflow_flow(action="finish")` 的证据聚合从"枚举 flow 各 step 的 child_session_key 凑 IN-list"改成"直接 `WHERE flow_id = ?`"。

**不为速度**(已有 `idx_evidence_session(session_id, event_id)` 索引,`session_id IN (...)` 已经是索引范围扫,提速微乎其微)。**为代码清晰度 + 鲁棒性**:
- 代码清晰:`WHERE flow_id = ?` 一行,免掉"枚举 step → 取各 child_session_key → 凑 IN-list"那段
- 鲁棒性:某个 step 的 `child_session_key` 跟踪丢/错也不漏证据(flow_id 盖章在记录侧,不依赖 step→session 反查)

**范围**:只动 TaskFlow 路径。todolist 校验波方案(`方案_最终校验波_FinalVerificationWave.md`)的 M3(descendant 枚举 + `read_for_keys`)不动 —— todo 无 flow,flow_id 帮不上 todolist 路径。两方案解耦。

## 现状

| 事实 | 证据 |
|------|------|
| `evidence_events` 无 `flow_id` 列 | `evidence_ledger.py:47-60` schema 只有 session_id |
| 已有索引 `idx_evidence_session(session_id, event_id)` | `evidence_ledger.py:61-62` |
| `taskflow_finish` 聚合:枚举 flow steps → 取各 child_session_key → `read_for_keys(session_keys=[...])` | `evidence_collector.py:18-34`、AGENTS.md "rows carry a session key, never a flow id" |
| **StateKey 无 FLOW_ID/STEP_ID** | `runtime/session/state_keys.py:181-182` 只有 REQUESTER_SESSION_KEY/CALLER_SCOPE |
| `spawn_subagent_direct` 不注 flow 上下文到子 session state | `spawn/core.py:514,626` 建 child_session_key 但不 set flow |
| `taskflow_step(action="resume")` 用 flow_id 查 flow 记录,不盖到子 session state | `taskflow_resume.py:59-60,115` |
| `evidence_recorder.record_verification_evidence` 不读 state_register | `evidence_recorder.py:48-83` 只取 session_id 参数 |

**核心缺口**:子 session 的 state_register **不知道自己跑哪个 flow**。flow_id 只存在 flow 记录里,链接是 flow→step→child_session_key(正向),反向(子 session→flow)没有。要盖章 flow_id 到证据行,必须先让子 session 的 state 携带 flow 上下文。

## 任务分解

### M1. 加 `StateKey.FLOW_ID`(+ `STEP_ID` 可选)

**文件**:`runtime/session/state_keys.py`

```python
class StateKey(str, Enum):
    ...
    REQUESTER_SESSION_KEY = "requester_session_key"  # 已有
    CALLER_SCOPE = "caller_scope"                     # 已有
    FLOW_ID = "flow_id"          # 新增:本 session 跑的 TaskFlow flow(子 session 才有,主会话 NULL)
    STEP_ID = "step_id"          # 新增:本 session 跑的 step(同上)
```

镜像 twin(`*_PENDING` / promote at turn boundary)按需 —— flow 上下文是 spawn 时一次性设的只读标记,不涉及 turn-boundary park/promote,不需要 twin。

### M2. spawn 路径注入 flow 上下文到子 session state

**文件**:`agent/tools/subagent/spawn/core.py`(`spawn_subagent_direct`)+ `agent/tools/taskflow/tools/_shared.py`(`step_spawn_kwargs` 合约)

`step_spawn_kwargs` 已携带 `response_schema`/`functional_role`/`step_model`/`step_timeout_seconds`。**加 `flow_id`/`step_id` 两个字段**:

```python
# _shared.py 的 step_spawn_kwargs 构造
step_spawn_kwargs = {
    "response_schema": step.response_schema,
    "functional_role": step.functional_role,
    "step_model": step.step_model,
    "step_timeout_seconds": step.step_timeout_seconds,
    "flow_id": flow_id,      # 新增
    "step_id": step.step_id,  # 新增
}
```

`spawn_subagent_direct` 接收这两个字段(spawn 合约透传,retry 继承,同 `response_schema` 的透传逻辑),在创建子 session 后**set 到子 session 的 state_register**:

```python
# spawn/core.py spawn_subagent_direct 内,建 child_session_key 之后
from runtime.session.state_register import set_state
if flow_id:
    set_state(child_session_key, StateKey.FLOW_ID, flow_id)
    if step_id:
        set_state(child_session_key, StateKey.STEP_ID, step_id)
```

**非 taskflow 的 spawn**(普通 subagent、side chat)不传 flow_id,子 session state 这两 key 为空 —— 正确,这些 session 的证据 flow_id 列为 NULL,不进 `WHERE flow_id = ?` 查询。

### M3. `evidence_events` 加 `flow_id` 列 + 索引

**文件**:`agent/tools/pub_base/evidence_ledger.py`(`_SCHEMA`)

```sql
CREATE TABLE IF NOT EXISTS evidence_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event TEXT,
    file_path TEXT,
    command TEXT,
    status TEXT,
    exit_code INTEGER,
    kind TEXT,
    modified_files_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    legacy_key TEXT UNIQUE,
    flow_id TEXT                    -- 新增:本证据属于哪个 flow(NULL = 非 taskflow 命令)
);
CREATE INDEX IF NOT EXISTS idx_evidence_session
    ON evidence_events(session_id, event_id);
CREATE INDEX IF NOT EXISTS idx_evidence_timestamp
    ON evidence_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_evidence_flow      -- 新增
    ON evidence_events(flow_id, event_id);
```

**迁移**:`ALTER TABLE evidence_events ADD COLUMN flow_id TEXT`(安全,默认 NULL;老行 NULL)。走 `_migration_ddls()` hook(`BaseSQLiteRepository` 已容错"duplicate-column error is the expected no-op")。新索引 `CREATE INDEX IF NOT EXISTS` 幂等。

### M4. `evidence_recorder` 记录时读 state 盖 flow_id

**文件**:`agent/tools/pub_base/evidence_recorder.py`(`record_verification_evidence`)+ `evidence_ledger.py`(`append`/`_insert`)

`record_verification_evidence(session_id, ...)` 现在只取 session_id 参数。加一步:从 state_register 读 `StateKey.FLOW_ID`:

```python
def record_verification_evidence(session_id, *, command, exit_code, ...):
    from runtime.session.state_register import get_state
    flow_id = get_state(session_id, StateKey.FLOW_ID, "") or ""
    fields = {
        "session_id": session_id,
        "command": command,
        "exit_code": exit_code,
        "status": "passed" if exit_code == 0 else "failed",
        "flow_id": flow_id or None,   # 新增:NULL for 非 taskflow 命令
        ...
    }
    EvidenceLedger.for_session(session_id).append(**fields)
```

`append`/`_insert` 的 INSERT 语句加 `flow_id` 列(`evidence_ledger.py:229-230` 的列列表 + 占位符)。

**成本**:每次 terminal/python_repl 调用多一次 `get_state`(state_register 是进程内内存查,微秒级),可接受。

**NULL 覆盖**:
- 主会话跑命令(flow_id 未设)→ NULL ✓
- 非 taskflow 子代理跑命令 → NULL ✓
- taskflow step 子 session 跑命令(M2 已设)→ 有值 ✓

### M5. `read_for_flow(flow_id)` 查询方法

**文件**:`agent/tools/pub_base/evidence_ledger.py`

```python
@classmethod
def read_for_flow(cls, flow_id: str) -> list[EvidenceRecord]:
    """读一个 flow 的全部证据(走 idx_evidence_flow 索引)。"""
    with cls._sync_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_events WHERE flow_id = ? ORDER BY event_id ASC",
            (flow_id,),
        ).fetchall()
    return [cls._record(r) for r in rows]
```

`evidence_collector.py` 暴露同名转发(同 `read_for_keys` 的转发模式)。

### M6. `taskflow_finish` 改用 `read_for_flow` 主 + `read_for_keys` 兜底

**文件**:`agent/tools/taskflow/evidence_collector.py`(聚合入口)+ `agent/tools/taskflow/tools/taskflow_finish.py`

当前聚合:`read_for_keys(session_keys=[creator_session, *step_child_sessions])`。

改为**hybrid**:

```python
async def aggregate_flow_evidence(flow_id, session_keys) -> list[EvidenceRecord]:
    """优先 flow_id 查;老行(flow_id NULL)靠 session_keys 兜底。"""
    by_flow = read_for_flow(flow_id)                    # 新路径:有 flow_id 的行
    flow_session_ids = {r.session_id for r in by_flow}
    legacy_keys = [k for k in session_keys if k not in flow_session_ids]
    by_session = read_for_keys(session_keys=legacy_keys) if legacy_keys else []
    return by_flow + by_session
```

**为什么 hybrid 不直接切**:`ALTER TABLE ADD COLUMN` 让老行 flow_id = NULL,`read_for_flow` 查不到它们。老行靠原 `read_for_keys` 路径兜底,直到 retention purge 把它们清掉(M3 迁移后,新行都有 flow_id,老行逐步淘汰)。一段时间后 legacy_keys 恒空,`read_for_keys` 分支自然死,可删。

## 不做什么

| 不做 | 理由 |
|------|------|
| 加 `todo_ref` 列 | 填值机制脆(agent 传参合规 or 状态跟踪 active_todo);大覆盖 NULL;查询还得回退 session_id。todolist 校验波方案的 M3 用 descendant 枚举已解决,不靠 schema |
| 改 todolist 校验波方案的 M3 | todo 无 flow,flow_id 帮不上 todolist 路径;两方案解耦 |
| 删 `read_for_keys` / `idx_evidence_session` | 兜底老行 + 非 taskflow 路径(主会话、普通 subagent)还要用;session_id 查询是通用兜底 |
| 加速度承诺 | 已有 session_id 索引,`WHERE flow_id = ?` vs `WHERE session_id IN (...)` 同阶,不提速。本方案是代码清晰度 + 鲁棒性收益 |

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | 子 session state 的 flow_id 设时机:spawn 时设,但 spawn 失败/retry 时状态竞态 | 中 | `set_state` 在 child_session_key 创建后立即设,retry 走同一路径重设(幂等);spawn 失败的 child 不进注册表,其证据也无记录 |
| R2 | 非 taskflow spawn 误传 flow_id(普通 subagent 也能传?) | 低 | `step_spawn_kwargs` 是 taskflow 专属合约;普通 subagent 走 `spawn_subagent_direct` 不传 flow_id 参数(默认 None) |
| R3 | evidence_recorder 读 state 失败(state_register 未初始化) | 低 | `get_state` 返回默认空串,flow_id 落 NULL,降级走 session_id 查询,不阻塞记录 |
| R4 | 老 SQLite 库迁移:加列 + 加索引 | 低 | `ALTER TABLE ADD COLUMN` + `CREATE INDEX IF NOT EXISTS` 幂等;`_migration_ddls` hook 已容错重复列 |
| R5 | hybrid 查询的语义:同一证据行会不会被 by_flow 和 by_session 双重读 | 中 | by_flow 先读出 flow_session_ids,by_session 只查不在其中的 legacy_keys,去重保证不双读 |
| R6 | 跨 flow 证据:一个子 session 跑了两个 flow 的 step?(理论不可能,但防御) | 低 | 一个 child_session_key 绑一个 step(M2 设一次);若重设,后设覆盖,以最后一次为准 |
| R7 | retention purge 清老行后 legacy_keys 恒空,`read_for_keys` 分支变死代码 | 低 | 死代码无害;一两个 release 后确认老行清完,单独 PR 删 hybrid 分支 |

## 落地顺序

1. **阶段 1**(state 基建):M1(`StateKey.FLOW_ID`/`STEP_ID`)+ M2(spawn 注入)。子 session state 携带 flow 上下文,但还没消费方 —— 安全,行为不变。
2. **阶段 2**(schema + 记录):M3(列 + 索引)+ M4(recorder 盖章)。新证据行带 flow_id,但查询侧还没用 —— 安全,查询仍走 session_id。
3. **阶段 3**(查询切换):M5(`read_for_flow`)+ M6(`taskflow_finish` hybrid)。开始消费 flow_id,老行靠 session_id 兜底。
4. **阶段 4**(清理):retention 清完老行后,删 hybrid 分支(`read_for_keys` 在 finish 路径的死代码)。

阶段 1-2 可单独发布(无行为变化);阶段 3 才切换查询路径;阶段 4 是收尾。

## 测试清单

### 阶段 1
- [ ] `StateKey.FLOW_ID`/`STEP_ID` 定义,默认空
- [ ] `spawn_subagent_direct` 接受 flow_id/step_id,设到子 session state
- [ ] 非 taskflow spawn 不传 flow_id,子 session state 这两 key 为空
- [ ] retry 子 session 重设 flow_id(幂等,后设覆盖)

### 阶段 2
- [ ] `evidence_events` 加 `flow_id` 列,老库迁移成功(老行 NULL)
- [ ] `idx_evidence_flow` 索引建好
- [ ] taskflow step 子 session 跑 terminal → 证据行 flow_id 有值
- [ ] 主会话跑 terminal → 证据行 flow_id NULL
- [ ] 普通 subagent 跑 terminal → 证据行 flow_id NULL

### 阶段 3
- [ ] `read_for_flow(flow_id)` 返回该 flow 全部证据
- [ ] `taskflow_finish` hybrid:新行(flow_id 有值)走 by_flow,老行(NULL)走 by_session
- [ ] hybrid 不双读(by_session 只查 legacy_keys)
- [ ] flow 的某个 step child_session_key 跟踪丢 → by_flow 仍能查到该 step 子 session 的证据(鲁棒性验证)
- [ ] 非 taskflow 路径(普通 subagent)证据不被 `read_for_flow` 误查(flow_id NULL)

### 阶段 4
- [ ] retention 清完老行后,`taskflow_finish` 的 legacy_keys 恒空
- [ ] 删 hybrid 分支后,`read_for_flow` 单查,行为不变

## 与其他方案的关系

| 方案 | 关系 |
|------|------|
| `方案_最终校验波_FinalVerificationWave.md`(todolist 校验波) | **解耦** —— todolist 路径 todo 无 flow,本方案帮不上;todolist 的 M3 用 descendant 枚举 + `read_for_keys` 不动 |
| `计划_打包_塞包模式_Electron式.md`(塞包) | 无关 —— 打包是 Tauri/构建层,证据台账是 agent 层 |
| `计划_打包_后置_MCP文件夹化.md`(MCP 文件夹) | 无关 —— MCP 是工具加载层 |

本方案是 TaskFlow 证据台账的**独立重构**,可单独推进,不依赖也不阻塞其他方案。
