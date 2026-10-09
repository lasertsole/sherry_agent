# 计划：TaskFlow 循环闭环（自动 resume + replan + goal judge）

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：TaskFlow 的执行底子很厚（DAG / 双层 judge / retry / evidence / finish gates / continuation），但 plan-execute 循环没闭环——每个步骤完成多花一轮 AI turn 调 resume + dispatch；replan 全靠 AI 自觉；没有目标级判定
- 对标：langchain-dynamic-workflow（5/5，plan=脚本，content-hash journal 零成本 resume）和 oh-my-openagent senpi-task（5/5，plan=声明式 DAG，amendRun 算传递依赖失效 + 自动 dispatch ready 节点）

## 现状对照

| 循环环节 | Sherry 现状 | senpi-task（5/5） | langchain-dwf（5/5） |
|---|---|---|---|
| 执行/分发 | ✅ run_task + dispatch | ✅ frontier admission 自动 | ✅ parallel/dag/pipeline |
| DAG 依赖 | ✅ depends_on + unlock | ✅ topological + wave | ✅ Kahn |
| 结果注入 | ⚠️ AI 手动调 resume | ✅ 自动结算 | ✅ journal 自动 |
| 结果评估 | ✅ 双层 judge | ✅ node 状态机 | ✅ schema + budget |
| 失败重试 | ✅ retry_policy + judge RETRY | ✅ retry + promptChanged | ✅ resume |
| **自动 wave 推进** | ❌ unlock 不 dispatch | ✅ 自动 | ✅ 自动 |
| **自动 replan** | ❌ 有能力无触发器 | ✅ amendRun 传递失效 | ⚠️ re-author 脚本 |
| 手动 replan | ✅ update_steps（全量替换） | ✅ amendRun | ✅ resume |
| 完成验证 | ✅ Gates A-D | ✅ diagnostics | ✅ journal + budget |
| 续跑防停 | ✅ TodoContinuationEnforcer | ✅ lease + reattach | ✅ checkpoint |
| 成本/截止 | ✅ token/cost + deadline | ✅ runStats | ✅ budget |
| **goal 级判定** | ❌ 只检查结构完整性 | ❌ | ❌ |
| **程序化分解** | ❌ AI 手写 step 列表 | ❌（LLM 声明 DAG） | ✅ LLM 写脚本 |

**结论：不照搬任何项目。** Sherry 的 TaskFlow 底子比两个 5/5 项目都厚（双层 judge、evidence ledger、finish gates A-D、knowledge extraction、cost tracking），缺的是循环闭环的 3 个触发器。补齐它们不重写底层——复用 DAG、judge、retry、evidence、finish gates、continuation。

---

## 方案一：自动 resume + auto-dispatch（最高性价比，~100 行）

### 问题

子代理完成后，announce pipeline 把结果投递回主 agent 的对话流，但**不自动调 `taskflow_resume`**。AI 必须读注入的结果 → 决定 → 手动调 `taskflow_resume(flow_id, child_key, result)` → 再调 `taskflow_dispatch` 分发下一 wave。每个步骤完成 = 2 轮 AI turn（resume + dispatch）白烧。

10 个 step 的 flow：原需 20+ 轮 AI turn → 修复后 1 轮（创建+全量 dispatch）+ 异常时几轮。**省 80%+ token。**

### 设计

#### 1a. announce 自动 resume

`agent/tools/subagent/announce/delivery.py` 投递子代理完成结果前，检测 flow 的 `creator_session_key`（flow state 里已有此字段，`default_state()` 的 docstring 明说"for cross-session auto-resume injection"但没人调）。

```python
# delivery.py，投递前检查
async def _try_auto_resume(run: SubagentRunRecord) -> bool:
    """自动把子代理结果注入 TaskFlow。返回 True = 已注入，AI 不需要再调 resume。"""
    try:
        from agent.tools.taskflow.registry.store_sqlite import get_flow_for_child_session
        from agent.tools.taskflow.tools.taskflow_resume import _resume_internal
        flow = get_flow_for_child_session(run.child_session_key)
        if flow is None:
            return False  # 不是 TaskFlow 分发的子代理
        _resume_internal(flow.flow_id, run.child_session_key, run.completion.result_text)
        return True
    except Exception:
        logger.debug("auto-resume failed", exc_info=True)
        return False  # fail-open：AI 手动 resume
```

只在 announce pipeline 的 delivery 前（`announce/core.py:76` merge 之后、`delivery.py` 投递之前）调一次。成功 = 结果已注入 flow state，AI 不需要再调 resume；失败 = fail-open 回退到当前行为（AI 手动调）。

#### 1b. taskflow_resume auto-dispatch

`agent/tools/taskflow/tools/taskflow_resume.py` 的 `unlock_dependents()` 返回新 ready 的 step_id 但不 dispatch。加 `auto_dispatch` 参数：

```python
def taskflow_resume(flow_id, child_session_key, result, *,
                    auto_dispatch: bool = True) -> str:
    # ... 现有 resume 逻辑 ...
    newly_ready = unlock_dependents(...)
    if auto_dispatch and newly_ready:
        for step_id in newly_ready:
            _dispatch.dispatch_child(flow_id, step_id, ...)
        return f"resumed + auto-dispatched {len(newly_ready)} step(s): {newly_ready}"
    return f"resumed, {len(newly_ready)} step(s) now ready: {newly_ready}"
```

默认 `auto_dispatch=True`——resume 后自动 dispatch ready 步骤。AI 只在 judge 说 RETRY/BLOCK 或需要 replan 时才收到注入的 turn。

#### 1c. announce 注入策略调整

自动 resume 成功时：
- judge PASS → 步骤 done，auto-dispatch 下一 wave → 如果所有步骤 done → 不注入 AI turn（flow 自动推进到完成）
- judge RETRY → 步骤重新 dispatched（带 judge feedback）→ 不注入 AI turn（自动重试）
- judge BLOCK → 步骤 blocked → **注入 AI turn**（需要 AI 决定 replan 或 cancel）
- 步骤 done 且有新 wave → auto-dispatch → 不注入 AI turn
- 步骤 done 且无新 wave 但 flow 未 finish → **注入 AI turn**（需要 AI 调 taskflow_finish 或 replan）

### 验收

- 10 步线性 flow（无依赖分支）：AI 调 1 次 create + 10 次 run_task → 10 个子代理完成后全部 auto-resume → flow 自动推进到 done → AI 只在最后调 1 次 finish。**12 轮 AI turn → 2 轮**（create+dispatch + finish）
- judge RETRY 一次：auto-resume 触发重试 → 不注入 AI turn → 重试成功 → auto-dispatch 下一 wave
- judge BLOCK：auto-resume 不 dispatch → 注入 AI turn → AI 决定 replan
- auto-resume 失败：fail-open → 投递结果给 AI → AI 手动调 resume（当前行为，无回归）

### 风险

- **auto-dispatch 的并发安全**：多个子代理同时完成时，`taskflow_resume` 的乐观锁（`expected_revision`）已有 conflict-retry 循环——auto-dispatch 复用同一路径，安全
- **AI 丧失中间可见性**：auto-resume 跳过了 AI 读结果。缓解：resume 的返回值仍进 tool result（WS 帧可见），trajectory 事件（如果做了方案四）也记录。AI 在最终 finish 时读全部 step results
- **fail-open 的级联风险**：auto-resume 失败 → 回退到手动 → 无回归。但如果 auto-resume 成功但 auto-dispatch 失败 → 步骤 done 但下一 wave 没 dispatch → AI 看到 "N steps ready" → 手动调 dispatch。需确保 auto-dispatch 失败时返回的 resume 结果包含 "dispatch failed" 提示

---

## 方案二：taskflow_replan 工具（~200 行）

### 问题

`taskflow_update_steps` 能力存在（全量替换 DAG + 6 条安全规则），但**没有触发器**。AI 要自己写完整的 step 列表替换——工作量大、容易遗漏依赖。

### 设计

新增 `taskflow_replan` 工具：

```python
@tool("taskflow_replan")
def taskflow_replan(
    flow_id: str,
    reason: str,               # 为什么 replan（失败原因 / 假设变更）
    blocked_step_id: str | None = None,  # 触发 replan 的 step
    keep_done: bool = True,    # 保留已完成步骤（默认 True）
    session_id: SessionId = "",
) -> str:
    """Replan the remaining (non-done) steps of a TaskFlow.

    Calls an auxiliary LLM with the current DAG + the failing step's result +
    judge feedback, regenerates the non-done steps preserving done/dispatched
    ones, and applies via taskflow_update_steps. Use when a step fails
    irrecoverably or results invalidate downstream assumptions.
    """
```

#### 流程

1. 读当前 flow state → 提取 DAG（steps + results + dependencies）
2. 分离 done/dispatched（保留）和 blocked/ready/failed（重新生成）
3. 如果 `blocked_step_id` 有值：读该 step 的 result + judge feedback
4. 调辅助 LLM（temperature 0）：
   - 输入：原始 goal + 当前 done 步骤的 results + 失败 step 的 result + reason
   - 输出：JSON step 列表（task + depends_on + 可选 response_schema/judge_criteria）
   - 约束：新 step 的 `depends_on` 只能引用 done 步骤或其他新 step；不能引用 dispatched（正在跑的）
5. 合并 done 步骤 + 新步骤 → 调 `taskflow_update_steps`（复用 6 条安全规则 + 乐观锁）
6. 计算传递失效（借鉴 senpi-task 的 amendRun）：
   - 如果 replan 改了 step X 的 `task`，X 的所有传递下游（递归 `depends_on` 遍历）标 `blocked`
   - blocked 步骤等新 step 完成后 unlock
7. 自动 dispatch 新 ready 步骤（复用方案一的 auto-dispatch）

#### 借鉴 senpi-task 的传递失效

```python
def _transitive_dependents(steps: list[dict], changed_id: str) -> set[str]:
    """所有直接或间接依赖 changed_id 的 step_id。"""
    direct = {s["step_id"] for s in steps if changed_id in (s.get("depends_on") or [])}
    all_deps = direct.copy()
    frontier = direct
    while frontier:
        next_frontier = set()
        for s in steps:
            if s["step_id"] in frontier:
                continue
            if any(d in frontier for d in (s.get("depends_on") or [])):
                next_frontier.add(s["step_id"])
        all_deps |= next_frontier
        frontier = next_frontier
    return all_deps
```

replan 改了 step X → `_transitive_dependents(steps, X)` 返回的步骤全部标 `blocked` → 等 X 重新完成后 unlock。

### 验收

- step-3 失败 → `taskflow_replan(flow_id, reason="step-3 failed: schema mismatch", blocked_step_id="step-3")` → LLM 重新生成 step-3 + 其下游 → done 的 step-1/step-2 保留 → step-3 重新 dispatched → 下游 blocked 等 unlock
- 传递失效：step-3 改了 task → step-4/step-5（依赖 step-3）标 blocked → step-3 重新完成后 unlock step-4/step-5
- 保留 dispatched：正在跑的 step 不被 replan 碰（安全规则 2）
- 保留 done：已完成的 step 的 task/depends_on 不变（安全规则 3）

### 风险

- **LLM 重新生成的 step 列表质量**：可能遗漏依赖或生成不合理的 step。缓解：`taskflow_update_steps` 的 6 条安全规则已经校验（unique id、depends_on 存在、无自依赖、dispatched 不降级、done 不改）；LLM 只生成非 done 部分，done 的保留
- **replan 循环**：replan 后新 step 也失败 → 再次 replan → 无限循环。缓解：每个 step 的 `retry_policy.max_retries` 限制重试次数；replan 本身也应该有次数上限（加 `replan_count` 到 flow state，超限标 flow `failed`）

---

## 方案三：taskflow_plan 分解 + goal judge（~300 行）

### 问题

plan 生成全靠 AI 自觉写 step 列表。没有结构化分解工具。`taskflow_finish` 只检查结构完整性（所有 step done），不检查语义目标满足。

### 设计

#### 3a. taskflow_plan 分解工具

```python
@tool("taskflow_plan")
def taskflow_plan(
    goal: str,               # 用户原始请求
    context: str | None = None,  # 可选：上下文文件路径或摘要
    max_steps: int = 10,     # 上限
    session_id: SessionId = "",
) -> str:
    """Decompose a goal into a dependency-ordered step list and create a
    TaskFlow with all steps registered. Each step carries task, depends_on,
    and optional response_schema/judge_criteria. Review the plan before
    dispatching with taskflow_run_task or taskflow_dispatch.
    """
```

流程：
1. 调辅助 LLM（temperature 0）：
   - 输入：goal + context + max_steps
   - 输出：JSON step 列表 `[{step_id, task, depends_on, response_schema?, judge_criteria?}]`
   - 约束：depends_on 只能引用更早的 step；无循环；step_id 唯一
2. `taskflow_create(flow_id=auto, description=goal)`
3. 批量 `taskflow_run_task`（注册但不 dispatch——只注册 blocked/ready，让 AI review 后 dispatch）

AI 可以在 dispatch 前修改 step 列表（`taskflow_update_steps`）再 dispatch。

#### 3b. Gate E — goal judge

`taskflow_finish` 加可选的 Gate E（当 flow 的 `description` 非空时触发）：

```python
def _goal_gate(flow_state) -> str | None:
    """Gate E: LLM judges whether the original goal is met.
    Returns None = pass; returns str = rejection reason (triggers replan)."""
    try:
        from agent.tools.subagent.spawn.completion_judge import _judge_with_llm
        results = [r["result"] for r in flow_state.get("results", [])]
        verdict = _judge_with_llm(
            criteria=flow_state["description"],  # 原始 goal
            result="\n---\n".join(results),     # 所有 step 结果
            model=None,  # 辅助模型
        )
        if verdict == "GOAL_NOT_MET":
            return "Goal not met: " + verdict.reason
        return None  # pass
    except Exception:
        return None  # fail-open
```

Gate E 在 Gates A-D 之后：
- A-D pass + E pass → `done`
- A-D pass + E fail → **拒绝 finish**，注入 replan 指令（调 `taskflow_replan`）
- E fail-open（模型错误）→ pass（不让 finish 卡死）

### 验收

- `taskflow_plan("fix bug #123", context="tests/test_bug.py")` → 生成 5 步 plan → AI review → dispatch → 全部完成 → `taskflow_finish` → Gate E 判 "bug 修了吗" → PASS → done
- Gate E 判 "GOAL_NOT_MET" → 拒绝 finish → 注入 replan 指令 → AI 调 `taskflow_replan`
- `taskflow_plan` 生成的 step 列表不合理 → AI 用 `taskflow_update_steps` 修改后再 dispatch

### 风险

- **goal judge 的假阳性/假阴性**：LLM 可能说 "GOAL_MET" 但实际没达标（假阳性 = 过早完成）或说 "GOAL_NOT_MET" 但实际达标了（假阴性 = 无限 replan）。缓解：fail-open 到 pass（不卡死）；replan 次数上限
- **plan 分解质量**：LLM 可能生成过于粗粒度或过于细粒度的 step。缓解：`max_steps` 上限；AI review 后 dispatch（不自动 dispatch）

---

## 推荐顺序

1. **方案一（auto-resume + auto-dispatch，~100 行）**：最高性价比，省 80%+ AI turn。改 `announce/delivery.py` + `taskflow_resume.py`。**先做这个。**
2. **方案二（taskflow_replan，~200 行）**：补齐 replan 触发器。复用 `taskflow_update_steps` 的安全规则，借鉴 senpi-task 的传递失效。**方案一落地后做。**
3. **方案三（taskflow_plan + goal judge，~300 行）**：补齐 plan 生成和 goal 判定。**最后做——前两个让循环跑起来，这个让它自主。**

**总成本：~600 行**，让 TaskFlow 从"AI 手动驱动的 DAG 工具"变成"AI 监督的自主 plan-execute 循环"。底层的 DAG、judge、retry、evidence、finish gates、continuation 全部复用——不重写。

## 不做的事

- **不照搬 langchain-dynamic-workflow 的"plan=脚本"范式**——sherry 的 TaskFlow 是声明式 DAG（更安全、更可审计），不换成 LLM 写编排代码
- **不照搬 senpi-task 的完整 WAL journal**——sherry 的 SQLite `state_json` + 乐观锁已够用，不需要 17 种 schema 版本的事件流
- **不重写底层**——DAG 调度、judge、retry、evidence、finish gates、continuation 全部复用
- **不做全自动**——AI 始终在监督位：auto-resume 只在 PASS/RETRY 时跳过 AI turn，BLOCK/fail/finish/goal-not-met 时仍注入 AI turn

## 关键文件路径

| 文件 | 角色 | 方案 |
|---|---|---|
| `agent/tools/subagent/announce/delivery.py` | 修改：投递前 auto-resume | 一 |
| `agent/tools/subagent/announce/core.py:76` | 修改：调 auto-resume 的时机 | 一 |
| `agent/tools/taskflow/tools/taskflow_resume.py` | 修改：加 `auto_dispatch` 参数 | 一 |
| `agent/tools/taskflow/registry/store_sqlite.py` | 参考：`get_flow_for_child_session` 查找 | 一 |
| `agent/tools/taskflow/tools/_dispatch.py` | 复用：`dispatch_child` 分发 | 一 |
| `agent/tools/taskflow/tools/taskflow_replan.py` | 新建：replan 工具 | 二 |
| `agent/tools/taskflow/tools/taskflow_update_steps.py` | 复用：6 条安全规则 + 乐观锁 | 二 |
| `agent/tools/taskflow/_shared.py` | 新增：`_transitive_dependents` | 二 |
| `agent/tools/taskflow/tools/taskflow_plan.py` | 新建：分解工具 | 三 |
| `agent/tools/taskflow/tools/taskflow_finish.py` | 修改：加 Gate E | 三 |
| `agent/tools/subagent/spawn/completion_judge.py` | 复用：LLM judge 调用 | 三 |
| `agent/tools/catalog.py` | 修改：注册 taskflow_replan + taskflow_plan | 二/三 |
