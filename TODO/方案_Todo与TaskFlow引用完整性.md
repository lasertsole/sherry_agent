# 方案:Todo↔TaskFlow 引用完整性(防孤儿 / 防重复 flow)

## 设计目标

修补动态改 todo(`todowrite` 全量替换)破坏 todo↔flow 1:1 引用的三个缺口:

1. **删除无 guard**:`update_todos` 全量替换会静默删掉 flow 的 GC root todo(只挡"标完成",不挡"删")
2. **无创建去重**:`taskflow_flow(action='create')` 不查"是否已有同目的非 terminal flow" → 重复建
3. **sweep grace window 内重复干活**:孤儿 flow 要等 sweep grace window 过才回收,窗口内可能派子 agent 跑重复工作

三道补丁,defense-in-depth:**P1 堵源头(删除时即时补偿)** + **P2 堵创建侧(去重检查)** + **P3 兜底(全量替换后立即触发 sweep)**。

## 现状缺口(已核对代码)

| 环节 | 现状 | 证据 |
|------|------|------|
| flow 记录存 todo_id? | **不存** —— 引用单向(todo→flow via flow_id) | `taskflow/registry/store_sqlite.py:4-9` schema 无 todo_id 列 |
| `update_todos` 行为 | **全量替换**(DELETE + INSERT) | `service.py` docstring "Full-replace";`store_sqlite.py:197-211` `replace_all` |
| 删除 guard | **只有"标完成"guard**(`_assert_transition_allowed`),无"删 flow_id todo"guard | `service.py` `if todo.get("status") == "completed": await _assert_transition_allowed(...)` |
| `todowrite` 暴露 flow_id? | **不暴露**(签名只有 `plan_ref`) | `todowrite.py:38` |
| 创建时 bind 失败补偿 | `compensate_flow_without_todo` 立即 fail flow | `_shared.py:68-84` |
| **删除时补偿** | **无即时补偿**,只靠 sweep grace window 后回收 | `compensate_flow_without_todo` 注释 "the unreferenced-flow sweep sees a flow with no active reference and collects it after the grace window" |
| 创建 flow 去重 | **无** | `taskflow_create.py` / `taskflow_plan.py` 不查已有同目的 flow |
| 无引用 sweep | 存在,带 grace window(`updated_at` 不活跃) | `taskflow/registry/store_sqlite.py:127-613` |

## 任务分解

### P1. `update_todos` 加删除补偿(auto-cancel + warning)

**文件**:`agent/tools/todolist/service.py`(`update_todos`)+ `agent/tools/taskflow/tools/_shared.py`(新 `compensate_orphaned_flows`)

全量替换前,算出"旧 todo 引用、新 todo 不再引用的活跃 flow_id",对这些 flow 做即时补偿(不等 sweep):

```python
# service.py update_todos 内,replace_all 之前
async def update_todos(session_id, todos, plan_ref=None):
    validated = _validate_todos(todos)
    for todo in validated:
        if todo.get("status") == "completed":
            await _assert_transition_allowed(todo, session_id)

    # P1 新增:算孤儿 flow,即时补偿
    old_todos = await store.get_todos(session_id)
    old_flow_ids = {t["flow_id"] for t in old_todos if t.get("flow_id")}
    new_flow_ids = {t.get("flow_id") for t in validated if t.get("flow_id")}
    orphaned = old_flow_ids - new_flow_ids  # 旧有引用、新列表不再引用的 flow
    cancelled_flows: list[dict] = []
    if orphaned:
        cancelled_flows = await compensate_orphaned_flows(
            session_id, list(orphaned), reason="todo_dropped_in_full_replace"
        )

    await store.replace_all(session_id, validated)
    # ... plan_ref / push ...
    latest = await store.get_todos(session_id)
    if cancelled_flows:
        # warning 附在返回里,让模型知道哪些 flow 被自动取消了
        logger.warning(
            "update_todos auto-cancelled {} orphaned flow(s): {}",
            len(cancelled_flows), [f["flow_id"] for f in cancelled_flows],
        )
    await _push_todo_update(session_id, latest)
    return latest
```

**`compensate_orphaned_flows`**(新,放 `_shared.py`,镜像 `compensate_flow_without_todo` 但批量):

```python
async def compensate_orphaned_flows(
    session_id: str, flow_ids: list[str], reason: str
) -> list[dict]:
    """对全量替换丢掉的 flow 做即时补偿:cancel(fail)非 terminal 的,跳过已 terminal 的。

    Fail-open:单个 flow 补偿失败不阻塞替换,sweep 是最终兜底。
    返回实际被 cancel 的 flow 列表(供调用方记 warning)。
    """
    cancelled: list[dict] = []
    for fid in flow_ids:
        flow = await store_sqlite.get_flow(fid, session_id)
        if not flow or _is_terminal(flow):
            continue  # 不存在或已 terminal,跳过
        try:
            await store_sqlite.fail_flow_for_gc(flow, reason=reason)
            cancelled.append(flow)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TaskFlow '{}' orphaned by todo full-replace but compensate failed: {}",
                fid, exc,
            )
            # sweep 兜底,不阻塞
    return cancelled
```

**模式可选**(config flag):
- `TODOLIST_INFRA["orphan_flow_on_drop"] = "cancel"`(默认,本方案的 auto-cancel + warning)
- `= "refuse"` —— 更严:返回错误"flow F 仍活跃,先 taskflow_flow(action='finish'/'cancel')再改 todo",不让替换落库。适合要显式控制的部署
- `= "sweep_only"` —— 关闭即时补偿,纯靠 sweep(回到现状,留作回退开关)

### P2. `taskflow_flow(action='create'/'plan')` 加去重检查

**文件**:`agent/tools/taskflow/tools/taskflow_create.py` + `taskflow_plan.py`

创建/规划 flow 前,查本 session 有没有**非 terminal 且 goal/description 相似**的 flow:

```python
async def _check_duplicate_flow(session_id, goal, description) -> dict | None:
    """查本 session 有没有非 terminal 且 goal/description 相似的 flow。"""
    active_flows = await store_sqlite.get_active_flows_for_session(session_id)
    target = _normalize_text(goal or description or "")
    if not target:
        return None
    for flow in active_flows:
        if _is_terminal(flow):
            continue
        existing = _normalize_text(flow.get("goal") or flow.get("description") or "")
        if not existing:
            continue
        # 相似度:子串包含 OR 归一化后完全相等 OR 简单 token overlap
        if target in existing or existing in target or _token_overlap(target, existing) > 0.7:
            return flow
    return None
```

匹配到则**不直接拒**,返回提示让模型决策:

```python
dup = await _check_duplicate_flow(session_id, goal, description)
if dup:
    return (
        f"Possible duplicate: flow '{dup['flow_id']}' (status={dup['status']}, "
        f"goal={dup.get('goal', '?')}) is already active and looks similar. "
        f"Reuse it (taskflow_step(action='resume', flow_id='{dup['flow_id']}', ...)) "
        f"or if you are sure this is a different task, re-call with explicit ack_dup=true."
    )
```

`taskflow_create`/`taskflow_plan` 入参加 `ack_dup: bool = False` —— 模型确认是不同任务时显式传 `ack_dup=true` 跳过检查(避免误判卡死)。

**相似度用文本**(子串 + token overlap),不调 LLM —— 创建路径要快,语义相似度太重。误判让模型 ack 跳过。

### P3. 全量替换后立即触发 sweep(兜底)

**文件**:`agent/tools/todolist/service.py`(`update_todos` 末尾)+ `agent/tools/taskflow/registry/store_sqlite.py`(sweep 触发入口)

P1 的 `compensate_orphaned_flows` 处理了**全量替换算出的孤儿**,但**漏网**(P1 补偿失败的、或非全量替换路径丢的)还靠 sweep。P3 把 sweep 的"等 grace window"改成"全量替换后立即跑一次":

```python
# service.py update_todos 末尾,replace_all + push 之后
if TODOLIST_INFRA.get("trigger_sweep_after_replace", True):
    from agent.tools.taskflow.registry import store_sqlite as tf_store
    # 即时扫一次孤儿 flow(不等 grace window)
    # sweep 本身幂等:已 terminal 的跳过,非 terminal 无引用的 fail
    try:
        await asyncio.to_thread(tf_store.run_unreferenced_flow_sweep, session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-replace unreferenced-flow sweep failed: {}", exc)
        # 不阻塞 todo 替换
```

`run_unreferenced_flow_sweep(session_id)` 是 `store_sqlite.py` 已有 sweep 逻辑的 session 限定触发(原 sweep 是系统级定时跑,这里加个 session 级即时入口)。sweep 幂等,P1 已 cancel 的 flow 是 terminal,sweep 跳过 —— 不重复处理。

### P4. flow terminal → todo 即时通知(对称补 success + failure)

**文件**:`agent/tools/todolist/registry/store_sqlite.py`(新 `complete_todos_for_flows`)+ `agent/tools/todolist/service.py`(转发)+ `agent/tools/taskflow/tools/taskflow_finish.py`(success 即时调)+ fail/cancel 路径(failure 即时调)

**现状缺口(已核对代码)**:flow 终态对 todo 的通知**不对称且延迟**:
- **failure**:经 sweeper(`subagent/registry/sweeper.py:392` → `cancel_todos_for_flows`)延迟 cancel,**非 fail 即刻**;fail 后到 sweeper 跑之间 todo 存库值仍 pending(模型看 stale pending,可能以为还能继续)
- **success**:**完全不告诉 todo**(无 `complete_todos_for_flows` 等价物);GC root todo 存库值保持 pending,模型看 stale pending → 可能重复派 flow / PlanContinuationEnforcer 误推 continuation

且 `get_todos` 返回**存库 status** 不读时派生(`store_sqlite.py:215-224`),所以"通知"必须**写存库值**,不是读时计算。

#### P4a. `complete_todos_for_flows`(success 镜像 cancel)

**文件**:`store_sqlite.py` + `service.py`

镜像现有 `cancel_todos_for_flows`,但把 terminal-succeeded flow 的 GC root todo 标 completed:

```python
# store_sqlite.py
async def complete_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
    """成功 flow 的 GC root todo 标 completed(镜像 cancel_todos_for_flows 的失败路径)。
    只更新 pending/in_progress 的(已 completed/cancelled 的不动)。"""
    ids = [fid for fid in flow_ids if fid]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    await ensure_db()
    async with _connect() as db:
        cursor = await db.execute(
            f"UPDATE {TABLE_NAME} SET status = ? "
            "WHERE session_id = ? AND status NOT IN (?, ?) "
            f"AND flow_id IN ({placeholders})",
            ("completed", session_id, "completed", "cancelled", *ids),
        )
        await db.commit()
        return int(cursor.rowcount or 0)
```

```python
# service.py(转发,镜像 cancel_todos_for_flows 的 service 包装)
@staticmethod
async def complete_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
    completed = await store.complete_todos_for_flows(session_id, flow_ids)
    if completed:
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
    return completed
```

#### P4b. finish 成功路径即时调 complete_todos_for_flows

**文件**:`agent/tools/taskflow/tools/taskflow_finish.py`

flow finish 成功(flow 状态 → finished)后,**立即**调 `complete_todos_for_flows`,不等模型手动标、不等 sweeper:

```python
# taskflow_finish.py,finish 门全过后、返回成功结果前
from agent.tools.todolist.service import TodoService

flow_id_str = str(flow_id)
# ... flow 状态落 finished ...
try:
    completed = await TodoService.complete_todos_for_flows(
        session_id, [flow_id_str]
    )
    if completed:
        logger.debug("TaskFlow '{}' finished: {} GC root todo(s) auto-completed", flow_id_str, completed)
except Exception as exc:  # noqa: BLE001 - fail-open: 通知失败不阻塞 finish
    logger.warning("TaskFlow '{}' finished but todo auto-complete failed: {}", flow_id_str, exc)
    # sweeper + 模型手动标是兜底
```

**fail-open**:通知失败不阻塞 finish(flow 已成功是事实,todo stale 是次要问题,sweeper/模型兜底)。

#### P4c. fail/cancel 路径即时调 cancel_todos_for_flows

**文件**:`agent/tools/taskflow/tools/taskflow_finish.py`(fail 路径)+ `taskflow_flow(action="cancel")` 路径

当前 failure 靠 sweeper 延迟 cancel。P4c 改成 **fail/cancel 即刻调** `cancel_todos_for_flows`(不等 sweeper):

```python
# taskflow_finish.py fail 路径(flow 状态 → failed)+ taskflow cancel 路径
try:
    cancelled = await TodoService.cancel_todos_for_flows(
        session_id, [flow_id_str]
    )
    if cancelled:
        logger.debug("TaskFlow '{}' failed/cancelled: {} GC root todo(s) auto-cancelled", flow_id_str, cancelled)
except Exception as exc:  # noqa: BLE001
    logger.warning("TaskFlow '{}' failed but todo auto-cancel failed: {}", flow_id_str, exc)
    # sweeper 兜底
```

sweeper 仍跑(兜底 P4c 漏的 / 老代码路径),但 P4c 让常态 fail/cancel **即时**通知。

#### P4 的对称效果

| flow 终态 | 通知 todo | 触发者 | 时机 |
|----------|----------|--------|------|
| succeeded | pending/in_progress → **completed** | `taskflow_finish` success 路径调 `complete_todos_for_flows`(P4b) | **即时**(finish 成功那一刻) |
| failed | pending/in_progress → **cancelled** | fail/cancel 路径调 `cancel_todos_for_flows`(P4c) | **即时**(fail/cancel 那一刻) |
| (兜底) | sweeper 跑时扫 terminal 的 | `subagent/registry/sweeper.py:392` | 延迟(P4 漏的 / 老代码) |

通知后 `get_todos` 返回更新后的存库值(模型看到 completed/cancelled,不再 stale pending)。

## 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | P1 默认 `cancel` 模式(auto-cancel + warning),不是 `refuse` | `refuse` 会卡死模型重写 todo(必须先 finish/cancel flow 才能改 todo);`cancel` 让模型自由重写,孤儿 flow 即时清,warning 给反馈 |
| D2 | P1 补偿 fail-open(单个 flow 补偿失败不阻塞替换) | 替换是模型主操作,不能因补偿失败卡住;sweep 兜底 |
| D3 | P2 不直接拒重复,返回提示 + `ack_dup` 跳过 | 误判会卡死创建;让模型决策,显式 ack 跳过 |
| D4 | P2 相似度用文本(子串 + token overlap),不调 LLM | 创建路径要快;语义相似度太重且慢;误判靠 ack 兜 |
| D5 | P3 即时触发已有 sweep,不新写回收逻辑 | sweep 已存在且幂等;P3 只是提前触发,改动最小 |
| D6 | 三道补丁 defense-in-depth,可独立部署 | P1 堵源头(删除补偿);P2 堵创建侧(去重);P3 兜底(即时 sweep)。任一道单独上都有用,叠加更稳 |
| D7 | P1/P3 都 fail-open,不阻塞 todo 替换 | todo 替换是模型主操作,引用完整性是后台关注,不能反过来卡模型 |
| D8 | **P4 对称通知**:success 即时 `complete_todos_for_flows`,failure 即时 `cancel_todos_for_flows`,都不等 sweeper | 现状不对称(success 不告诉 / failure 延迟);`get_todos` 返回存库值不派生,必须写存库才反映;即时通知堵 stale pending(成功后模型看 pending → 重复建 flow / 误推 continuation);sweeper 仍跑作兜底 |
| D9 | P4 fail-open(通知失败不阻塞 finish/fail) | flow 终态是事实(门已判),todo stale 是次要;sweeper + 模型手动标兜底;不反过来卡 flow 终态落库 |

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | P1 auto-cancel 误杀:模型临时重写 todo(打算加回来),flow 被即时 cancel | 中 | warning 反馈让模型知道;cancel 是 fail flow 不是 delete,可重开;`refuse` 模式给严格部署 |
| R2 | P1 算孤儿用集合差(old - new),但模型可能在替换里**重写**了同 flow_id 的 todo(理论上模型设不了 flow_id,但防 bug) | 低 | 集合差正确:新列表无该 flow_id 即算孤儿;模型设不了 flow_id,新列表的 flow_id 只能来自 `bind_flow_todo`(append,不走 replace) |
| R3 | P2 文本相似度误判:goal 措辞不同但同任务 → 漏判;措辞相似但不同任务 → 误判 | 中 | 漏判靠 P1/P3 兜底;误判靠 `ack_dup` 跳过;相似度阈值可配 |
| R4 | P2 `ack_dup=true` 被模型滥用(每次都 ack 跳过去重) | 低 | ack 是显式信号,可日志审计;滥用时去重形同虚设但 P1/P3 仍兜底 |
| R5 | P3 即时 sweep 与系统级定时 sweep 竞态(同 flow 被两边扫) | 低 | sweep 幂等(已 terminal 跳过,非 terminal fail);`fail_flow_for_gc` 用 expected_revision 乐观锁防并发 |
| R6 | P1 `compensate_orphaned_flows` 调 `fail_flow_for_gc`,但 flow 可能有派生子 agent 在跑 | 中 | `fail_flow_for_gc` 应触发子 agent suppress/kill(同 cascade delete 路径);需确认 fail 路径含子 agent 清理 |
| R7 | 全量替换高频触发 P1+P3,每次都扫 flow,性能 | 低 | 单 session 活跃 flow 通常个位数;`get_active_flows_for_session` 走索引;P3 sweep session 限定,不扫全库 |
| R8 | P4b success 通知把 GC root todo 标 completed,但 flow 可能"门过但实际没干对"(门判有误)→ todo 误标完成 | 中 | 门判是 finish 的 A-C+E(证据无 FAIL + goal judge),已是多层校验;若门判漏,P4b 信任它(同现状 barrier 信任);校验波方案(`方案_最终校验波`)的 HITL 终审是更深兜底 |
| R9 | P4 success/failure 通知与 sweeper 竞态(同 flow 两边都更新 todo) | 低 | `complete_todos_for_flows`/`cancel_todos_for_flows` 的 WHERE 都带 `status NOT IN ('completed','cancelled')`,已终态的跳过,幂等 |
| R10 | P4b 在 finish 成功路径调 todolist service(跨 store),事务边界 | 低 | 通知在 flow 状态落库之后(finish 已 commit),通知失败 fail-open 不回滚 flow 状态;todo 更新是独立事务 |

## 落地顺序

1. **阶段 1(P3 兜底,最省改动)**:全量替换后即时触发 sweep。用已有 sweep 逻辑,只加触发入口。**单独上即能缩短孤儿窗口**,作为 P1 的安全网先部署。
2. **阶段 2(P1 主补)**:`update_todos` 加 `compensate_orphaned_flows`。堵源头,孤儿 flow 即时 cancel。默认 `cancel` 模式 + warning。
3. **阶段 3(P2 创建去重)**:`taskflow_create`/`taskflow_plan` 加去重检查 + `ack_dup`。防"换 todo 重建同 flow"的特定场景。
4. **阶段 4(P4 终态对称通知)**:`complete_todos_for_flows`(success 镜像)+ finish 成功路径即时调 + fail/cancel 路径即时调 `cancel_todos_for_flows`。堵 stale pending(成功不告诉 / 失败延迟)。

阶段 1 最先(改动最小、即见效);阶段 2 堵源头;阶段 3 防创建侧;阶段 4 补终态通知对称。四阶段独立可发布,叠加成完整防线。

## 测试清单

### 阶段 1(P3)
- [ ] `update_todos` 全量替换后,`run_unreferenced_flow_sweep(session_id)` 被调用
- [ ] 替换丢掉 flow_id todo → 即时 sweep 把该 flow 标 failed(不等 grace window)
- [ ] sweep 幂等:已 terminal 的 flow 跳过;P1 已 cancel 的不重复处理
- [ ] sweep 失败不阻塞 todo 替换(fail-open)

### 阶段 2(P1)
- [ ] 全量替换丢掉 flow_id todo → `compensate_orphaned_flows` cancel 该 flow
- [ ] 已 terminal 的 flow 跳过补偿
- [ ] 补偿失败不阻塞替换(fail-open),sweep 兜底
- [ ] 返回的 warning 列出被 cancel 的 flow_id
- [ ] `refuse` 模式:替换被拒,返回"先 finish/cancel flow F"
- [ ] `sweep_only` 模式:回到现状(P1 关闭,P3 仍跑)
- [ ] flow_id todo 在新列表里保留 → 不算孤儿,不补偿
- [ ] P1 cancel 的 flow 触发子 agent suppress/kill(R6)

### 阶段 3(P2)
- [ ] 创建 flow 时,本 session 有同 goal 非 terminal flow → 返回提示
- [ ] `ack_dup=true` 跳过去重检查
- [ ] 相似度:子串包含 / token overlap > 0.7 判重
- [ ] 已 terminal 的 flow 不算重复
- [ ] 不同 session 的同 goal flow 不算重复(去重是 session 级)

### 阶段 4(P4 终态对称通知)
- [ ] `complete_todos_for_flows` 把 terminal-succeeded flow 的 GC root todo 标 completed(只更新 pending/in_progress)
- [ ] `taskflow_flow(action="finish")` 成功 → 即时调 `complete_todos_for_flows`(不等模型手动标、不等 sweeper)
- [ ] finish 成功后 `get_todos` 返回的 GC root todo 是 completed(不再 stale pending)
- [ ] fail/cancel 路径即时调 `cancel_todos_for_flows`(不再等 sweeper 延迟)
- [ ] fail 后 `get_todos` 返回的 GC root todo 是 cancelled(即时,非延迟)
- [ ] `complete_todos_for_flows` / `cancel_todos_for_flows` 幂等(已 completed/cancelled 的跳过,WHERE status NOT IN ...)
- [ ] 通知 fail-open 不阻塞 finish/fail(通知失败 flow 状态仍落库)
- [ ] sweeper 仍跑,扫到 P4 已处理的 terminal flow 跳过(幂等)

### 端到端(四阶段叠加)
- [ ] 模型全量替换丢掉 flow_id todo → P1 即时 cancel + warning → flow 不再派子 agent
- [ ] 模型随后 `taskflow_flow(action='create')` 同目的 → P2 提示已有(若 P1 未 cancel 完)/ 允许新建(P1 已 cancel)
- [ ] 全程无重复 flow 派子 agent 干同样活
- [ ] todo 列表重写自由,不被引用完整性卡死(fail-open + warning)
- [ ] flow finish 成功 → P4b 即时把 GC root todo 标 completed → 模型 `get_todos` 看到 completed(不 stale pending)→ 不重复建 flow / 不误推 continuation
- [ ] flow fail → P4c 即时把 GC root todo 标 cancelled → 模型看到 cancelled(不以为还能继续)

## 与其他方案的关系

| 方案 | 关系 |
|------|------|
| `方案_最终校验波_FinalVerificationWave.md`(todolist 校验波) | **协同** —— 校验波方案给 todo 加 `kind`/`accept_criteria`,本方案不改 todo schema,只加删除/创建 guard。两方案可并行,不冲突 |
| 证据台账 flow_id（已落地） | **解耦** —— 那是证据表加列,本方案是 todo↔flow 引用完整性。两者都涉及 flow_id 但不同层 |
| `计划_打包_塞包模式_Electron式.md` / MCP 文件夹化 | 无关 —— 打包/工具层,不碰引用完整性 |

本方案是 todo↔TaskFlow 引用完整性的**独立补丁集**,可单独推进。
