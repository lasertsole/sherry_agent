# 方案:Todolist 最终校验波(Final Verification Wave)

## 设计哲学

校验波**原生在 todolist 里**,**只覆盖纯 todolist 路径(无 TaskFlow flow 的会话)**。Sherry 的 todolist 是用户可见的轻量清单 + `todo_updated` 推送 + PlanContinuationEnforcer("计划 = todos")门;证据台账是 **session 级** always-on 记 terminal/python_repl 输出,不依赖 TaskFlow。这俩是抓手 —— 校验 todo 带 `kind="verification"` + `accept_criteria`,完成时查证据台账(命令类)或注入偏执 reminder(人工类),最后 HITL 真人终审。

**不覆盖 TaskFlow 路径**:走 TaskFlow 的会话(有活跃 flow)靠 flow 内部校验链,不叠校验波。理由 —— TaskFlow 内部校验已完整且**对齐 todo 目标**:
- `bind_flow_todo` 创建时把 todo content 链接到 flow 的 `description`(`taskflow_create.py:121`)
- Gate E 判 flow 的 `description`(`taskflow_finish.py:62`)—— **判的就是 todo 的目标**
- replan **不改 `description`**(`taskflow_replan.py:50,102` 保留原 goal,只重规划 steps)→ 全生命周期对齐
- 加上证据门 C(flow 聚合证据无 FAIL)+ 回归门(finish 重放验证命令)+ StepJudge(每 step 的 validation_criteria)+ CompletionJudge(goal 循环)

→ flow-level GC root todo 的 barrier(`flow terminal? 是→放行`)信任这条链;校验波叠上去是**重复判同个 `description` + 重复查证据**,冗余。所以:**有活跃 TaskFlow flow → 跳过校验波(信任 Gate E);无 flow(纯 todolist)→ 启用校验波**(那里没有内部校验,模型标完成无校验)。

不照搬 oh-my 的文本协议门(`## Final Verification Wave` + `VERDICT: APPROVE` 正则)。oh-my 的校验 todo 状态是 agent 自己标 `[x]`(可骗);Sherry 用证据台账 + HITL 真人审,不可骗。

## 现状盘点

### todolist 已有

| 字段/机制 | 位置 | 作用 |
|-----------|------|------|
| `position` 列 | `store_sqlite.py:6` | todo 排序(PK 之一),决定"最后节点" |
| `category` 列 | `store_sqlite.py:6` | 委托词表(默认 'quick'),非实现-vs-校验语义 |
| `plan_ref` 列 | `store_sqlite.py:8` | 链接计划文件 |
| `flow_id`/`step_id` 列 | `store_sqlite.py:7` | 链接 TaskFlow step(**本方案不用**) |
| `todo_updated` WS push | `service.py` | 前端实时更新 |
| PlanContinuationEnforcer (E3) | `agent/middlewares/todo_continuation/core.py:3` | "gate covers the whole PLAN: todos + flows" —— 计划完成门 |
| TodoService.create / update_todos | `service.py:240+` | 增删改 todo |

### 证据台账(session 级,不依赖 TaskFlow)

| 机制 | 位置 | 作用 |
|------|------|------|
| always-on 记录 | `agent/tools/pub_base/sqlite_store.py` | 每次 terminal/python_repl 工具调用自动记 SQLite(WAL) |
| session_key 索引 | `evidence_collector.py:18,23` | 按 session_key 查;`taskflow_finish` 聚合只是传多个 key,记录本身是 session 级 |
| 命令 + 输出 + 通过/失败/stale/broken | 同上 | 校验 todo 完成时可查"本 session 有没有匹配的通过命令" |

### 缺什么

| 缺项 | 本方案怎么补 |
|------|-------------|
| todo 区分"实现"vs"校验" | todolist 加 `kind` 列(`'implementation'\|'verification'`) |
| 校验 todo 的完成标准 | todolist 加 `accept_criteria` 列(命令+期望,或人工检查+期望观察) |
| 校验 todo 完成时的硬检查 | 完成门:命令类查证据台账,人工类注入偏执 reminder |
| 计划完成门卡校验 | PlanContinuationEnforcer 扩展:所有 `kind="verification"` todo 必须 `completed` |
| 终审 | HITL 中断(复用 `exit_plan_mode` 的 interrupt 合约) |

## 设计

### M1. todolist 加 `kind` + `accept_criteria` 列

**文件**:`agent/tools/todolist/registry/store_sqlite.py`(建表 + 迁移)+ `service.py`(create/update 透传)

```sql
ALTER TABLE todos ADD COLUMN kind TEXT NOT NULL DEFAULT 'implementation';
ALTER TABLE todos ADD COLUMN accept_criteria TEXT;  -- NULL for implementation todos
```

```python
class TodoKind(str, Enum):
    IMPLEMENTATION = "implementation"  # 默认
    VERIFICATION = "verification"       # 校验 todo,触发完成门 + HITL 终审
```

**迁移**:SQLite `ALTER TABLE ADD COLUMN ... DEFAULT 'implementation'`;老 todo 默认实现,行为不变。`accept_criteria` 老行为 NULL(不触发校验门)。

### M2. 计划 `## Verification` 段 → todolist 校验项

**文件**:`agent/tools/plan_mode/exit_plan_mode.py`(模板)+ 新 `agent/tools/plan_mode/verification_parser.py`

计划模板的 `## Verification` 段(已存在,`exit_plan_mode.py:64`)从散文升级为结构化:

```markdown
## Verification
- [ ] V1. 跑全量测试: `uv run pytest tests/ -q` — accept: 退出码 0,0 失败
- [ ] V2. 类型检查: `uv run basedpyright agent/` — accept: 退出码 0
- [ ] V3. lint: `uv run --with ruff ruff check .` — accept: 退出码 0
- [ ] V4. 手动跑后端: `uv run python -m server` — accept: 8080 端口监听,启动日志无 ERROR
```

`exit_plan_mode` 审批通过后(`PLAN_STATUS_READY`),解析 `## Verification` 段的 `- [ ] V\d+\.` 项 → 创建 todolist 项:

```python
{
    "content": "V1. 跑全量测试: uv run pytest tests/ -q",
    "kind": "verification",
    "accept_criteria": {"command": "uv run pytest tests/ -q", "expect": "exit_code:0", "type": "command"},
    "position": <last + 1>,  # 排在所有实现 todo 之后
}
# V4 类(无命令的人工检查):
{
    "content": "V4. 手动跑后端",
    "kind": "verification",
    "accept_criteria": {"type": "manual", "expect": "8080 端口监听,启动日志无 ERROR"},
    "position": <last + 1>,
}
```

**position 保证校验 todo 最后**:解析时 `position` 从现有 todo 数 +1 起递增,实现 todo 在前,校验 todo 在后。

### M3. 校验 todo 完成门(证据台账 + reminder)

**文件**:`agent/tools/todolist/service.py`(update_todos 完成时检查)+ `agent/middlewares/todo_continuation/`(reminder 注入)

当 agent 标 `kind="verification"` 的 todo 为 `completed` 时,**完成门前置检查**:

#### 命令类(`accept_criteria.type == "command"`)

查证据台账:本 session **及其子代理 session** 有没有匹配 `accept_criteria.command` 且结果"通过"的记录。

**关键事实**(核对 `agent/tools/pub_base/evidence_ledger.py:47-60` 的 schema):证据行按 `session_id` 存,**没有 `flow_id` 列**。`evidence_collector.read_for_keys(session_keys=[...])` 接受多个 session_key,`taskflow_finish` 聚合 flow 证据时就靠它把各 step 的 `child_session_key` 凑成列表传进去。todolist 路径同样要传多 session —— 主 session + 本会话派生的子代理 session(子代理跑的校验命令记在子 session_id 下,单查主 session 查不到,见 R7)。

```python
async def _check_verification_evidence(session_id, criteria) -> tuple[bool, str]:
    """查证据台账:主 session + 子代理 session 有没有匹配命令且通过的记录。

    复用 evidence_collector.read_for_keys(同 taskflow_finish 聚合路径),
    不自己写单 session 查询 —— 单查主 session 会漏掉子代理跑的命令。
    """
    from agent.tools.taskflow.evidence_collector import read_for_keys
    from agent.tools.subagent.registry import enum_descendant_session_keys  # 新 helper

    # 主 session + 本会话派生的所有子代理 session(子代理跑的命令记在子 session_id 下)
    session_keys = [session_id, *enum_descendant_session_keys(session_id)]
    rows = read_for_keys(session_keys=session_keys)

    cmd = criteria["command"]
    # 命令匹配用子串容错(agent 可能加 flag,如 criteria 是 "pytest" 实跑 "uv run pytest -q")
    matched = [r for r in rows if cmd in (r.command or "")]
    passing = [r for r in matched if r.passed and not r.stale and not r.broken]
    if passing:
        return True, f"evidence matched in session {passing[0].session_id}"
    if matched and not passing:
        return False, f"evidence for {cmd} exists but stale/broken — re-run"
    return False, f"no passing evidence for {cmd} — run it first (main or subagent)"
```

**子代理 session 枚举**:`enum_descendant_session_keys(session_id)` 是新 helper(放 `agent/tools/subagent/registry/`),遍历 subagent 注册表的本会话子树,返回所有 child `session_key`。subagent 注册表已跟踪 parent→child(用于 cascade delete、drain),枚举是已有数据的只读投影,不引入新状态。

- 有匹配的通过记录(主或子代理跑的)→ 允许完成
- 无 → **拒绝完成**,todo 状态保持 `in_progress`,返回提示"先跑 `uv run pytest tests/ -q` 再标完成(可主会话或派子代理跑)"
- 有匹配但 stale/broken → 拒绝 + 提示"证据过期/破坏,重跑"

#### 人工类(`accept_criteria.type == "manual"`)

不查证据(没法自动查),但**注入偏执 reminder**(M6 的 `VERIFICATION_REMINDER` 等价物),逼 agent 真去做检查:

```python
# 标记人工类校验 todo 完成时,注入 reminder(不硬拒,但逼重验)
inject_verification_reminder(session_id, todo_content, accept_criteria)
# reminder 内容见 M6
```

### M4. PlanContinuationEnforcer 扩展:校验 todo 全完成(**纯 todolist 路径,有 flow 跳过**)

**文件**:`agent/middlewares/todo_continuation/core.py`

当前门"todos + flows"。扩展:在判断"计划是否完成"时,**所有 `kind="verification"` 的 todo 必须 `completed`** —— **但只在纯 todolist 路径(无活跃 TaskFlow flow)启用**。有活跃 flow 的会话靠 flow 内部校验(Gate E 等),跳过校验 todo 门(信任 Gate E,不冗余判):

```python
async def _plan_complete(session_id, session_todos) -> bool:
    """计划完成判定。路径分流:有活跃 flow → 信任 Gate E(跳过校验 todo 门);无 flow → 启用校验 todo 门。"""
    from agent.tools.taskflow.registry import store_sqlite as tf_store

    # 路径分流:本 session 有非 terminal flow → TaskFlow 路径,信任 Gate E
    active_flows = await tf_store.get_active_flows_for_session(session_id)
    has_active_flow = any(not _is_terminal(f) for f in active_flows)

    impl_done = all(t.status == "completed" for t in session_todos if t.kind == "implementation")

    if has_active_flow:
        # TaskFlow 路径:flow 的 Gate E 判目标(= todo content,replan 不改,对齐),证据门 C + 回归门查证据
        # 校验 todo 门跳过(冗余);计划完成 = 实现 todo 完成(flow 的 finish 门另判 flow 目标)
        return impl_done

    # 纯 todolist 路径:无 flow 内部校验,启用校验 todo 门
    verify_todos = [t for t in session_todos if t.kind == "verification"]
    verify_done = all(t.status == "completed" for t in verify_todos) if verify_todos else True
    return impl_done and verify_done
```

- 无校验 todo 的会话 → 行为不变(向后兼容)
- **有活跃 TaskFlow flow** → 跳过校验 todo 门(信任 Gate E;校验 todo 即使未完成也不阻塞,因为 flow 的 finish 门另判)
- **无活跃 flow(纯 todolist)** → 有校验 todo 未完成 → 计划未完成,enforcer 继续推 agent;全完成 → 触发 M5 HITL 终审
- flow 从有到无(finish/cancel 后)→ 自动切回纯 todolist 路径(若还有校验 todo 未完成,继续推)

**flow 生命周期与校验门的切换**:会话中途可能从"有 flow"切到"无 flow"(flow finish/cancel),或反向(create flow)。`_plan_complete` 每次判定都重查活跃 flow,**动态分流**,不缓存路径。

### M5. HITL 终审(全校验 todo 完成后)

**文件**:新 `agent/tools/todolist/final_approval.py` + PlanContinuationEnforcer 集成

所有 `kind="verification"` todo `completed` 后,**不直接算计划完成**,挂 HITL 中断(复用 `exit_plan_mode` 的 interrupt 合约):

```python
if all_verification_todos_completed and not already_approved:
    raise Interrupt(
        kind="todolist_final_approval",
        payload={
            "verification_summary": [...],  # 每个校验 todo 的 content + 证据状态
            "evidence_summary": {...},       # 证据台账摘要
        },
    )
```

前端弹卡片:
```
┌─────────────────────────────────────────┐
│ 最终校验通过 — 最终审批?               │
│                                         │
│ 校验项:                                 │
│ ✓ V1. 全量测试 — 证据:退出码 0         │
│ ✓ V2. 类型检查 — 证据:退出码 0         │
│ ✓ V3. lint — 证据:退出码 0             │
│ ✓ V4. 手动跑后端 — agent 自验          │
│                                         │
│ [批准完成]  [驳回回计划]                │
└─────────────────────────────────────────┘
```

- **批准** → 计划标完成,PlanContinuationEnforcer 停推
- **驳回**(带反馈)→ 校验 todo 回 `pending`/`in_progress`,反馈注入下一轮

## 严格度校准(防过严 / 防过松)

omo 的核心策略:**严格度放流程,不放关卡**。关卡本身蠢,真严格度在 StepJudge prompt + 计划审查 + HITL。本方案照此,但把 StepJudge 换成"证据台账 + reminder"(todolist 轻量路径没 StepJudge)。

### 防过松:M6 偏执 reminder(人工类校验 todo)

**文件**:`agent/middlewares/todo_continuation/`(reminder 模板)

人工类校验 todo(`accept_criteria.type == "manual"`)完成时注入偏执 reminder(照 omo `VERIFICATION_REMINDER` 的精神):

```
You just marked a VERIFICATION todo as complete. The implementer (you or a subagent) claimed it's done. They are probably lying.

Before this counts as complete, you MUST:
- Run the actual check yourself (don't trust "I think it works")
- For user-facing changes: actually launch it, click through, check console
- "Probably" = NOT done. Investigate until certain.

If you cannot prove it works with tool output this turn, the todo stays in_progress.
```

**命令类校验 todo 不靠 reminder**(靠 M3 证据台账硬查),**人工类靠 reminder + M5 HITL 真人审**(因为没法自动查)。这是"机械硬 + 语义软"的分工。

**反 trivial**:`accept_criteria` 的 `expect` 必须具体("退出码 0"/"8080 端口监听"),不是"看看正常"。命令类 todo 完成时证据台账查的是**命令真的跑过 + 通过**,不是 agent 说"我跑了"。

### 防过严:M7 计划审查者 permissive

**文件**:新 `agent/middlewares/plan_review/`(或 exit_plan_mode 集成)+ 计划审批 HITL 前

照 omo momus:计划审查者**反完美主义**:

- **80% 阈值**:计划 80% 清楚就 OKAY,不卡小瑕疵
- **三档裁决**:OKAY(默认)/ ITERATE(可补,最多 3 个,planner 自修,最多 2 轮)/ REJECT(硬阻塞:引用文件不存在 / 任务无法启动 / 自相矛盾 / 需用户决策)
- **只看硬阻塞,不判质量**:不评最优解/架构/性能(除非明确破坏)
- **模糊 `## Verification` 项 = 阻塞**(M8):"verify it works"/"check the page" 在计划审查阶段就拒

### 防过松在计划侧:M8 criteria 具体性

**文件**:`verification_parser.py`(M2)+ 计划审查者(M7)

`## Verification` 段每项必须有:
- **命令类**:具体命令 + 具体期望(`accept: 退出码 0` / `accept: 0 失败`),不是"accept: 通过"
- **人工类**:具体期望观察(`accept: 8080 端口监听,启动日志无 ERROR`),不是"accept: 正常"

计划审查者(M7)拒绝模糊项:"verify it works"/"确认一下"/"check the page" = REJECT,进不了校验波。

### 关卡保持蠢(防过严)

M3 完成门 + M4 计划门都只查 `kind="verification"` todo 的 `status` 是否 `completed`(命令类额外查证据台账匹配),**不查输出内容/措辞**。真严格度在:
- 上游:M7 计划审查(宽松,防过严)+ M8 criteria 具体性(防过松)
- 中游:M3 证据台账硬查(命令类,防过松)+ M6 reminder(人工类,防过松)
- 下游:M5 HITL 真人审(防过松,人环路)

关卡不脆性耦合到特定输出字符串(omo 的 `VERDICT: APPROVE` 文本匹配就是这种脆性)。

## 数据流总览

**路径分流**:`exit_plan_mode` 批准后,若 agent 用 TaskFlow(`taskflow_flow(action='create')`)→ TaskFlow 路径(flow 内部 Gate E + 证据门 C + 回归门 + StepJudge,对齐 todo 目标,不叠校验波);若 agent 用纯 todolist(plain todo)→ 校验波路径(M3/M4/M5)。

```
用户提需求 → enter_plan_mode → 规划(读+搜索)
  │
  ├─ 写计划:## Goal / ## Scope / ## Steps / ## Verification / ## Risk
  │  (## Verification 段列 V1./V2./V3./V4. + accept 标准)
  │
  └─ exit_plan_mode → HITL 暂停(计划审批)
       │  ↑ M7 计划审查者先跑(permissive:80% 过,模糊项拒)
       │
       批准 → PLAN_STATUS_READY
       │
       ├─ M2 解析 ## Steps → todolist 实现项(kind=implementation, position 在前)
       ├─ M2 解析 ## Verification → todolist 校验项(kind=verification, position 最后)
       │
       └─ agent 干活(PlanContinuationEnforcer 推)
            │
            ├─ 实现 todo 一个个完成
            ├─ 校验 todo:
            │    命令类 → agent 跑命令(terminal/python_repl)→ 证据台账 always-on 记录
            │           标 completed 时 M3 查证据台账:有匹配通过记录?有→过;无→拒,提示重跑
            │    人工类 → agent 自验 → 标 completed 时 M6 注入偏执 reminder 逼重验
            │
            └─ M4 PlanContinuationEnforcer:所有校验 todo completed?
                 │
                 └─ 是 → M5 HITL 终审暂停
                      │
                      批准 → 计划完成 ✓
                      驳回 → 校验 todo 回 pending,反馈注入
```

## 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 校验波**原生在 todolist**,不进 TaskFlow | 用户要求;todolist 是用户可见轻量清单,证据台账 session 级不依赖 TaskFlow,够用 |
| D2 | todo 加 `kind` + `accept_criteria` 两列,不复用 `category` | `category` 是委托词表(quick/complex),语义不同;`kind` 是实现-vs-校验,职责清晰 |
| D3 | 命令类校验靠证据台账硬查,人工类靠 reminder + HITL | 命令有客观输出可查(证据台账已 always-on 记);人工检查没法自动查,靠偏执 reminder 逼重验 + HITL 真人兜底 |
| D4 | `position` 保证校验 todo 最后 | 解析 `## Verification` 时 position 从实现 todo 数 +1 起;用户可见"最后几项是校验" |
| D5 | PlanContinuationEnforcer 扩展查校验 todo(**纯 todolist 路径;有 flow 跳过信任 Gate E**) | 已有"todos + flows"门;加路径分流:有活跃 flow → 跳过校验 todo 门(flow 的 Gate E 判目标 = todo content,对齐);无 flow → 启用校验 todo 门 |
| D6 | HITL 终审复用 `exit_plan_mode` 的 interrupt 合约 | 同构(批准/驳回带反馈),前端 HITL 卡片基建可复用 |
| D7 | 无校验 todo 的会话 → 行为不变 | 老会话/简单任务无 `kind="verification"` todo,M3/M4 跳过,向后兼容 |
| D8 | **严格度放流程,不放关卡** | M3/M4 关卡只查 status(+证据匹配),不查输出内容;真严格度在 M7(宽松,防过严)+ M8(具体性,防过松)+ M6(reminder,防过松)+ M5(HITL 真人) |
| D9 | **双向校准** —— 计划审查宽松(80% 过,三档),校验完成偏执(证据硬查 + reminder) | 计划阶段防过严,执行阶段防过松,两阶段方向相反互补 |
| D10 | **路径分流:有 TaskFlow flow → 跳过校验波(信任 Gate E)** | Gate E 判 flow `description`(= todo content,replan 不改,全周期对齐)+ 证据门 C + 回归门 + StepJudge;校验波叠上去重复判同 `description` + 重复查证据,冗余。M4 每次判定重查活跃 flow,动态分流不缓存 |
| D11 | **todo 侧证据校验 = 校验波 M3(verification todo 完成时查),不直接改 todo 完成路径** | plain todo 完成自由(不查证据);flow-level todo 完成只 barrier(flow terminal?信任 Gate E);证据查只在 `kind="verification"` todo 完成时(M3),scoped 到纯 todolist 路径。todo 完成路径本身不加证据门,靠校验波 M3 在 verification todo 上做 |
| D10 | `accept_criteria` 必须具体 | "verify it works" 计划阶段拒(防过松);命令类要有命令+期望,人工类要有期望观察 |

## 与 oh-my-openagent 的对比

| 维度 | oh-my | Sherry(本方案) |
|------|-------|-----------------|
| 校验在哪 | TodoWrite(=todolist)的 `## Final Verification Wave` 段 | todolist 的 `kind="verification"` 项 |
| 区分校验 | `F\d+.` 文本编号 | `kind` 结构化字段 |
| 完成判定 | agent 自己标 `[x]`(可骗) | 命令类:证据台账硬查;人工类:reminder + HITL 真人 |
| 关卡 | `VERDICT: APPROVE` 文本匹配 | M3/M4 查 status + 证据匹配(不查输出内容) |
| 终审 | agent 说 APPROVE | HITL 真人点批准 |
| 计划审查 | momus(permissive) | M7 计划审查者(permissive,80% 过) |
| 类型安全 | 文本协议 | schema 字段 + 枚举 |

**Sherry 强在哪**:校验 todo 完成不可骗(命令类证据台账硬查,人工类 HITL 真人审);oh-my 的 `[x]` 是 agent 自己标,可骗。

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | `kind`+`accept_criteria` 两列迁移:老库无此列 | 中 | `ALTER TABLE ADD COLUMN ... DEFAULT 'implementation'` / NULL;老 todo 默认实现,行为不变 |
| R2 | 证据台账匹配命令的精度:agent 跑了 `pytest` 但台账记的是 `uv run pytest` | 中 | 匹配用子串/正则容错;查 session 内所有匹配命令的通过记录,不只字面相等 |
| R3 | stale/broken 证据:命令跑过但后续文件改动让证据过期 | 中 | M3 查时排除 stale/broken;提示"证据过期,重跑" |
| R4 | 人工类校验 todo 无硬查,靠 reminder + HITL | 中 | 这是设计意图(没法自动查);reminder 偏执 + HITL 真人兜底;命令类能自动查的尽量用命令类 |
| R5 | PlanContinuationEnforcer 改动:加校验 todo 子条件 | 中 | 扩展不重构;`_plan_complete` 加一个子检查,无新门 |
| R6 | HITL 终审增加完成延迟 | 中 | 设计意图(最终人工把关);可配开关,批量任务可关 |
| R7 | 子代理跑校验命令:证据记在子 session_id 下,单查主 session 查不到 | 中 | 证据台账 schema **无 `flow_id` 列**,按 `session_id` 存(`evidence_ledger.py:47-60`);子代理跑的命令记在子 `session_key` 下。M3 复用 `evidence_collector.read_for_keys(session_keys=[主, *子代理])`,不自己写单 session 查询 —— 同 `taskflow_finish` 聚合 flow 证据的路径。新增 `enum_descendant_session_keys()` helper 从 subagent 注册表枚举本会话子树(只读投影,cascade delete 已跟踪 parent→child) |
| R8 | `## Verification` 段格式错 → 不创建校验 todo | 中 | 解析容错:格式不对记 warning,降级无校验 todo(M4 跳过),不阻塞 |

## 落地顺序

1. **阶段 1**(核心):M1(todolist `kind`+`accept_criteria` 列)+ M2(计划 `## Verification` 解析 → todolist 校验项)+ M4(PlanContinuationEnforcer 扩展查校验 todo)。地基,没它后面都白搭。
2. **阶段 2**(完成门 + 终审):M3(证据台账查 + reminder)+ M5(HITL 终审 + 前端卡片)。
3. **阶段 3**(严格度校准):M6(偏执 reminder)+ M7(计划审查者 permissive)+ M8(criteria 具体性)。可与阶段 2 并行(M6 依赖 M3,M7/M8 依赖 M2)。
4. **阶段 4**(可视化):前端 todolist 区分校验项 + 终审卡片 + 进度推送带 `kind`。

阶段 1 可单独发布(给纯 todolist 用户);阶段 2-4 增量叠加。

## 测试清单

### 阶段 1
- [ ] todolist 加 `kind`+`accept_criteria` 列,老 todo 默认 implementation,行为不变
- [ ] 计划 `## Verification` 段解析 → 创建 `kind="verification"` todo,position 最后
- [ ] 计划无 `## Verification` 段 → 不创建校验 todo(M4 跳过)
- [ ] PlanContinuationEnforcer:有校验 todo 未完成 → 计划未完成,继续推
- [ ] PlanContinuationEnforcer:无校验 todo → 行为不变(向后兼容)
- [ ] `todo_updated` push 带 `kind` 字段

### 阶段 2
- [ ] 命令类校验 todo 标 completed 时:证据台账有匹配通过记录 → 允许完成
- [ ] 命令类:无匹配 → 拒绝,提示"先跑 X 再标完成"
- [ ] 命令类:匹配但 stale/broken → 拒绝,提示"证据过期,重跑"
- [ ] 人工类校验 todo 标 completed 时:注入偏执 reminder
- [ ] 所有校验 todo completed → HITL 终审触发
- [ ] 批准 → 计划完成,PlanContinuationEnforcer 停推
- [ ] 驳回带反馈 → 校验 todo 回 pending,反馈注入
- [ ] **路径分流:有活跃 TaskFlow flow → 跳过校验 todo 门**(M4 信任 Gate E,校验 todo 即使未完成也不阻塞计划)
- [ ] **路径分流:无活跃 flow(纯 todolist)→ 校验 todo 未完成阻塞计划**
- [ ] **路径分流:flow 从有到无(finish/cancel)→ 切回纯 todolist 路径,若校验 todo 未完成继续推**
- [ ] **路径分流:flow 从无到有(create)→ 切到 TaskFlow 路径,跳过校验 todo 门**
- [ ] **M4 每次判定重查活跃 flow**(动态分流,不缓存路径)
- [ ] plain todo(无 kind)完成自由(不查证据,不限路径)
- [ ] flow-level todo(有 flow_id)完成只 barrier(信任 Gate E,不限路径)

### 阶段 3(严格度校准)
- [ ] M7 计划审查:80% 清楚 → OKAY;可补小问题 → ITERATE;硬阻塞 → REJECT
- [ ] M7 不判质量(架构/性能/最优解),只看硬阻塞
- [ ] M8:`## Verification` 有"verify it works" → 计划审查 REJECT
- [ ] M8:命令类无具体期望(只"accept: 通过")→ REJECT
- [ ] M8:人工类无期望观察(只"accept: 正常")→ REJECT
- [ ] M6 reminder 注入后,agent 重验(命令类靠证据,人工类靠自验)

### 阶段 4
- [ ] todolist UI 区分校验项(不同图标/颜色)
- [ ] 终审卡片四语言文案
- [ ] 进度面板显示校验项状态
