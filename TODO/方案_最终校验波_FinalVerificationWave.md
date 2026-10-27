# 方案:最终校验提醒(omo 式,极简)

## 设计哲学

**只做一件事**:todo 全完成时,注入一条偏执 reminder,提示 agent 验收结果。agent 自验(读改动文件 / 跑测试 / 查结果),信不信由它。**无证据台账查询、无 HITL、无 LLM goal 判官、无 snapshot 对比、无 plan_key、无任何 schema 改动。**

跟 omo(oh-my-openagent)的 `VERIFICATION_REMINDER` 同模式 —— 偏执 prompt 逼 agent 自验,不靠代码门硬查。omo 的 `VERDICT: APPROVE` 文本门也不要(那是脆性耦合,Sherry 不照搬)。Sherry 只取 reminder 的偏执精神。

## 废弃的复杂机制

校验波此前设计过的机制(M1-M8,本方案自己的)—— **只留 M6 reminder,其余废弃**:

| 机制 | 状态 | 为什么不做/保留 |
|------|------|----------------|
| M1 `kind="verification"` + `accept_criteria` 列 | 废弃 | 极简版不区分 verification todo;reminder 对所有 todo 统一提示,不查 schema |
| M2 plan `## Verification` 解析 → 校验 todo | 废弃 | 无 `kind` 列,不创建结构化校验 todo |
| M3 证据台账查(命令类硬门) | 废弃 | 复杂(证据 scope / stale 派生);reminder 代替,agent 自验跑命令 |
| M4 校验 todo 全完成门 | 废弃 | 不门控;用现有 PlanContinuationEnforcer 的"所有 todo completed",reminder 是提示不是 gate |
| M5 HITL 终审 | 废弃 | 不人工校验 |
| **M6 偏执 reminder** | **保留** | **本方案核心** —— todo 全完成时注入,逼 agent 自验 |
| M7 计划审查者(permissive LLM) | 废弃 | 省一次 LLM 调用;reminder 逼自验已够 |
| M8 criteria 具体性强制 | 废弃 | 无 `accept_criteria` 列,无需强制 |

**其它方案的机制(不是校验波自己的,单独说明)**:
- ~~M10 goal 判官 / M11 snapshot~~ —— 来自 `方案_校验波_目标判官与防gaming.md`(已删);校验波极简版不叠 goal 判官 / snapshot
- ~~`plan_key` 列~~ —— 来自 `方案_证据台账_加plan_key列.md`(已删);不查证据台账,不需要 scope
- `flow_id` 列 —— 来自 `方案_证据台账_加flow_id列.md`(**保留**,TaskFlow finish 门自己的事,跟校验波解耦)

**保留的相关方案**(跟校验波解耦,不动):
- `方案_证据台账_加flow_id列.md` + `方案_证据台账_finish门flow_idonly.md` —— TaskFlow finish 门用
- `方案_Todo加todo_id列.md` + `方案_Todo局部改API与孤儿复用.md` —— todo 身份 + 局部改 API
- `方案_Todo与TaskFlow引用完整性.md` —— todo↔flow 引用完整性

## 唯一改动:注入校验 reminder

**文件**:`agent/middlewares/todo_continuation/core.py`(PlanContinuationEnforcer)

PlanContinuationEnforcer 检测到"所有 todo completed + 无活跃 flow"时,在继续/完成指令里**附一条偏执 reminder**:

```python
# todo_continuation/core.py,plan_complete 判定后注入 reminder
_VERIFICATION_REMINDER = """
## 最终校验提醒

你的所有 todo 已标完成。但 agent 常在"实际没真做对"时标完成 —— 代码有错、测试 trivial、逻辑错、悄悄加没要求的功能。**假设你的工作是坏的,直到你自己证明 otherwise。**

验收前必做:
1. **读改动文件**:`git diff` 看改了什么,逐行确认符合任务要求。注意 stub/TODO/placeholder/scope creep。
2. **跑验证命令**:测试 / 类型检查 / lint。命令必须真跑过(`terminal` 工具),不能"我觉得能过"。
3. **手动检查(用户可见的改动)**:UI / CLI / API —— 实际跑一遍,看输出对不对。

"大概对" = 不对。"我觉得" = 没验。验到确定为止。

若发现问题,继续修(不要标完成);若确实全过,才算完成。
"""

if all_todos_completed and no_active_flow:
    # 注入 reminder 到 continuation/完成指令
    directive += _VERIFICATION_REMINDER
```

**触发条件**:
- 所有 todo `status == "completed"`(机械查,同现有 PlanContinuationEnforcer 的 plan_complete 判定)
- 无活跃 TaskFlow flow(有 flow 走 TaskFlow 自己的 Gate E,不叠 reminder)
- 每 plan 一次(不每轮重复 —— 同现有 continuation 节流)

**不门控**:reminder 是**提示**,不是 gate。agent 读完可能继续修(发现问题时),可能直接完成(确认全过时)。**不查证据台账、不调 LLM 判官、不挂 HITL、不对比 snapshot** —— 信不信由 agent。

## 跟 omo 的对比

| 维度 | omo | Sherry(本方案) |
|------|-----|-----------------|
| 校验触发 | `## Final Verification Wave` 段 + F-task | 所有 todo completed(PlanContinuationEnforcer 判定) |
| 校验内容 | `VERIFICATION_REMINDER`(4 阶段偏执 prompt) | 同款偏执 reminder(读改动 / 跑命令 / 手动检查) |
| 关卡 | `VERDICT: APPROVE/REJECT` 文本匹配门 | **无门**(只提示,agent 自验) |
| 终审 | agent 说 APPROVE | **无**(agent 自验后直接完成或继续修) |
| 证据台账 | 无独立台账 | 有(session 级 always-on,但校验波**不查** —— 留给 TaskFlow finish 门) |
| HITL | 无 | 无 |

Sherry 比 omo 更简:**连 VERDICT 文本门都不要**。只取 reminder 的偏执精神(假设坏了逼自验),不取脆性文本协议门。

## 为什么这样够

1. **agent 自验是主防线**:reminder 偏执("假设坏了"),agent 读改动 + 跑命令 + 手动查,这本身是有效校验(omo 验过)
2. **TaskFlow 路径有自己的硬门**:走 TaskFlow 的会话靠 Gate E(LLM goal 判)+ 证据门 C + 回归门 + StepJudge,不需要校验波叠
3. **纯 todolist 路径靠 reminder**:todo 全完成 → reminder 逼自验 → agent 发现问题继续修 / 确认全过才完成。无需代码门硬查,无需人审
4. **复杂机制的边际收益不值**:证据台账查(M3)、goal 判官(M10)、snapshot(M11)、plan_key scope —— 每个都加复杂度,但纯 todolist 路径的污染窗口窄(agent 自验能 catch 大部分),硬门的边际收益不抵维护成本

## 落地

**单一改动**:PlanContinuationEnforcer 加 reminder 注入。无 schema 改动、无新工具、无新列、无 LLM 调用、无 HITL。

1. 写 `_VERIFICATION_REMINDER` 常量(偏执 prompt,四语言或单语言先 ZH)
2. PlanContinuationEnforcer 的 plan_complete 分支注入 reminder
3. 节流:每 plan 一次(同现有 continuation 节流机制)
4. 测试:todo 全完成 + 无 flow → reminder 注入;有 flow → 不注入(走 Gate E)

## 测试清单

- [ ] 所有 todo completed + 无活跃 flow → reminder 注入到 continuation 指令
- [ ] 有活跃 TaskFlow flow → 不注入(走 Gate E)
- [ ] todo 未全完成 → 不注入(正常 continuation)
- [ ] 每 plan 只注入一次(不每轮重复)
- [ ] reminder 内容含"读改动 / 跑命令 / 手动检查"三步
- [ ] agent 读完 reminder 后继续修(todo 回 in_progress)→ 不再注入(未全完成)
- [ ] agent 确认全过 → 计划完成(无门拦)

## 与其他方案的关系

| 方案 | 关系 |
|------|------|
| ~~`方案_校验波_目标判官与防gaming.md`~~(已删除) | M10(goal 判官)/ M11(snapshot)全不做;本方案(reminder)替代 |
| ~~`方案_证据台账_加plan_key列.md`~~(已删除) | 不查证据台账,不需要 plan scope |
| `方案_证据台账_加flow_id列.md` + `finish门flow_idonly` | **保留** —— TaskFlow finish 门用,跟本方案解耦(TaskFlow 路径不注入 reminder) |
| `方案_Todo加todo_id列.md` + 局部改 API + 引用完整性 | **保留** —— todo 身份 / 局部改 / 引用完整性,跟本方案解耦 |
| 校验波此前的 M1-M8(除 M6 reminder) | **废弃** —— schema 改动 / 证据查 / 门 / 判官 / snapshot 全不做;只留 M6 的 reminder 精神 |

本方案是校验波的**极简重写**:只留 reminder(omo 式偏执提示),砍掉所有代码门 / 证据查 / schema 改动 / LLM 判官 / snapshot。简单、够用、不脆。
