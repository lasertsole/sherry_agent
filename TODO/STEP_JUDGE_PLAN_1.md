# TaskFlow Step Judge & 子代理完成门控计划

> 参考 hermes-agent `goals.py::judge_goal()` + `verification_evidence.py` + `kanban_tools.py` 的完成门控机制，为 Sherry 的 TaskFlow DAG 引擎和子代理系统增加程序级质量判别器。

---

## 1. 现状分析

### 1.1 hermes-agent 的判别器机制（三层）

| 层                           | 机制                                                                       | 文件                             | 性质                                     |
| ---------------------------- | -------------------------------------------------------------------------- | -------------------------------- | ---------------------------------------- |
| LLM Goal Judge               | `judge_goal()` — 辅助 LLM 评估任务是否完成                                 | `hermes_cli/goals.py:836-964`    | LLM 驱动，3 verdict (done/continue/wait) |
| Verification Evidence Ledger | `classify_verification_command()` — 按 exit code 记录 test/lint/build 结果 | `agent/verification_evidence.py` | 规则驱动，被动记录                       |
| Completion Gate              | `kanban_complete` 门控 — 调用 judge_goal，verdict≠done 则拒绝完成          | `tools/kanban_tools.py:595-624`  | 程序级门控                               |

**hermes-agent 的关键设计**：

- Judge 使用**独立辅助 LLM**（`get_text_auxiliary_client("goal_judge")`），非主对话模型
- Judge prompt 返回**单行 JSON**，温度=0，max_tokens=4096
- **Fail-open**：任何错误返回 "continue"，不阻塞进度
- Goal Loop：judge verdict=continue → 注入 continuation prompt → 继续下一轮，直到 verdict=done 或预算耗尽
- 完成合约（`GoalContract`）：outcome/verification/constraints/boundaries/stop_when，提供结构化验收标准

### 1.2 Sherry 当前机制（对比）

| 维度                | Sherry 现状                                                                      | hermes-agent                                | 差距         |
| ------------------- | -------------------------------------------------------------------------------- | ------------------------------------------- | ------------ |
| LLM 质量判定        | **无**。失败检测靠 `classify_failure()` 文本子串匹配（timeout/rate_limit/error） | `judge_goal()` LLM 语义判断                 | 完全缺失     |
| 验证证据跟踪        | `EvidenceLedger` + `SisyphusVerifier` **5-gate 的子集**（Gate1/Gate4 执行、Gate5 预留、Gate2 仅记录、Gate3 交 orchestrator）**存在但仅在测试中调用** | `verification_evidence.py` 生产路径自动记录 | 存在但未接入 |
| 完成门控            | `_assert_transition_allowed()` 检查存活/状态，**不检查质量**                     | `kanban_complete` 调 `judge_goal` 门控      | 无质量门控   |
| validation_criteria | 存储在 step 上，**仅回显为警告文本** "⚠ Result needs validation"                 | `GoalContract` 传入 judge prompt 强制评估   | 存在但未强制 |
| Goal Loop           | 无。子代理一次性执行，返回即完成                                                 | 循续轮驱动直到 done 或预算耗尽              | 完全缺失     |
| 提示级提醒          | `SubagentCompletionDrainMiddleware` 附加 `_VERIFICATION_REMINDER` 文本           | 无（用程序门控代替提示）                    | 提示 ≠ 强制  |

**核心问题**：Sherry 的完成验证策略是**提示驱动的编排**——SKILL.md 告诉 LLM 遵循 Sisyphus 合约，`SubagentCompletionDrainMiddleware` 附加文本提醒，但代码级强制仅限于存活/状态检查。SisyphusVerifier 已实现 **5-gate 的子集**——Gate1（plan 重读）执行、Gate4（taskflow step + subagent 存活）执行、Gate2（自动化命令）仅记录、Gate3（人工 QA）交 orchestrator、Gate5（cleanup receipts）预留——但未接入生产调用链。

---

## 2. 设计目标

1. **TaskFlow Step Judge**：在 `taskflow_resume` 注入子代理结果后，用辅助 LLM 评估结果是否满足 step 的 `validation_criteria`，返回 PASS/RETRY/BLOCK verdict
2. **子代理完成门控**：在 `complete_subagent_run()` 之前，调用 goal judge 评估子代理结果是否满足任务目标
3. **Goal Loop**：verdict=RETRY 时注入 continuation prompt，续轮驱动子代理直到 PASS 或预算耗尽
4. **Evidence Ledger 接入生产**：将 `SisyphusVerifier.verify()` 接入 `taskflow_finish` 前置门控
5. **Fail-open**：judge 不可用时不阻塞进度（与 hermes 一致）

---

## 3. 架构设计

### 3.1 三层判别器架构（对标 hermes-agent）

```
                    ┌──────────────────────────────────────────────────┐
                    │            TaskFlow DAG Engine                   │
                    │                                                  │
                    │  dispatch_child() ──► 子代理执行 ──► resume()   │
                    │                                                  │
                    │  resume() 后插入 StepJudge:                      │
                    │    ┌──────────────────────────────────────┐      │
                    │    │  Layer 1: StepJudge (LLM)            │      │
                    │    │  输入: step.task + step.criteria     │      │
                    │    │       + 子代理 result_text          │      │
                    │    │       + evidence_ledger 状态        │      │
                    │    │  输出: PASS / RETRY / BLOCK           │      │
                    │    │  模型: auxiliary_llm (温度=0)        │      │
                    │    └──────────┬───────────────────────────┘      │
                    │               │                                  │
                    │    PASS ──► step["status"]=DONE ──► unlock_deps()│
                    │    RETRY ──► retry_count++ ──► re-dispatch       │
                    │              (带 judge feedback)                 │
                    │    BLOCK ──► mark_step_blocked()                 │
                    │               (需人工或 orchestrator 介入)        │
                    │                                                  │
                    │  finish() 前置门控:                              │
                    │    ┌──────────────────────────────────────┐      │
                    │    │  Layer 2: SisyphusVerifier (规则)     │      │
                    │    │  Gate 1: plan reread                  │      │
                    │    │  Gate 2: evidence_ledger 有通过记录   │      │
                    │    │  Gate 4: 所有 step done + 无活跃子代理│      │
                    │    │  Gate 5: cleanup receipts 预留        │      │
                    │    └──────────┬───────────────────────────┘      │
                    │               │                                  │
                    │    全部通过 ──► allow finish                    │
                    │    任一失败 ──► reject finish (返回错误+原因)    │
                    └──────────────────────────────────────────────────┘
```

> **关于 `mark_step_done()`**：当前 `taskflow_resume` **不调用 `_shared.mark_step_done()`**，而是内联 `step["status"] = str(StepStatus.DONE)`（`agent/tools/taskflow/tools/taskflow_resume.py:148`）。`_shared.mark_step_done(steps, child_session_key)`（`agent/tools/taskflow/tools/_shared.py:138`）是既有幂等助手，Phase 1 可改用它替代内联赋值（不是必须，但更安全）。

### 3.2 子代理完成门控（Goal Loop 模式）

```
spawn_subagent_direct() ──► _execute_subagent() ──► 子代理第一轮执行
                                                         │
                                              ┌──────────▼───────────┐
                                              │  CompletionJudge     │
                                              │  (LLM, auxiliary)    │
                                              │  输入: task 描述     │
                                              │       + result_text  │
                                              │       + evidence     │
                                              │  输出: DONE/CONTINUE │
                                              └──────────┬───────────┘
                                                         │
                                        ┌────────────────┼────────────────┐
                                        │                │                │
                                   DONE              CONTINUE         budget 耗尽
                                        │                │                │
                              complete_run()    注入 continuation      block_run()
                              announce()        prompt ──► 下一轮       (人工审查)
                                        │
                              （StepJudge 在 resume 中
                               对 result 再做一次判定）
```

### 3.3 与现有中间件链的集成

| 集成点                | 位置                                                               | 修改类型                      |
| --------------------- | ------------------------------------------------------------------ | ----------------------------- |
| StepJudge             | `agent/tools/taskflow/tools/taskflow_resume.py:146-157` — 在内联设置 `step["status"]=DONE`（:148）之前 | 插入 judge 调用               |
| CompletionJudge       | `agent/tools/subagent/spawn/core.py::_execute_subagent()` — 在 `complete_subagent_run(...)` 之前（:733） | 插入 goal loop                |
| SisyphusVerifier 接入 | `taskflow_finish.py` — 在状态转换为 DONE 之前                      | 插入适配层门控                |
| Evidence Ledger 接入  | `terminal.py` / `python_repl.py` 工具执行后                        | 自动记录 exit code            |
| Evidence stale 标记   | `file_tools/write_file.py` / `file_tools/patch_file.py` 工具执行后 | 标记关联 evidence 为 stale    |
| Completion Drain 升级 | `subagent_completion_drain/core.py` — `abefore_model` hook         | 类属性 `enforce_verification` |

> **`_execute_subagent` 不写 `run.outcome`**：它在函数开头构造局部 `outcome = RunOutcome(status=RunOutcomeStatus.OK)`（`spawn/core.py:583`），在 `except` 分支重新赋值（:659-666），最后统一传给 `complete_subagent_run(run.run_id, outcome, result_text, expected_generation=run.generation)`（:733-737）。因此 Goal Loop 的插入点是 **`complete_subagent_run(...)` 之前**，改动的是这个**局部 `outcome`**（例如预算耗尽时置 `error="goal_loop_budget_exhausted"`），而不是 `run.outcome`。
>
> **`SubagentCompletionDrainMiddleware` 的实例化点**：**仅** `agent/core.py:180`（`_build_middlewares()` 内）。子代理中间件列表（`spawn/core.py:846-878`）**不含**它，故 Phase 4 的门控升级只影响主代理。

---

## 4. 实现计划

### Phase 1：StepJudge — TaskFlow 步骤级 LLM 判别器

#### Step 1.1：StepJudge 模块

- [ ] **新建 `agent/tools/taskflow/step_judge.py`**

```python
"""LLM-based step result judge for TaskFlow DAG.

Inspired by hermes-agent's judge_goal() (hermes_cli/goals.py:836-964).
Uses an auxiliary LLM to to evaluate whether a subagent's step result
satisfies the step's validation_criteria.

Design principles (inherited from hermes-agent):
- Fail-open: any error → verdict=PASS (never block progress on judge failure)
- Single-line JSON response (temperature=0)
- Auxiliary LLM (not the main conversation model)
- Evidence-aware: judge sees verification evidence status
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import NamedTuple

from loguru import logger

from models import build_auxiliary_llm  # agent → models 合法导入
from config.features import STEP_JUDGE  # 配置开关

class StepVerdict(StrEnum):
    PASS = "pass"
    RETRY = "retry"
    BLOCK = "block"

class JudgeResult(NamedTuple):
    verdict: StepVerdict
    reason: str
    feedback: str  # injected into retry prompt; empty on PASS

JUDGE_SYSTEM_PROMPT = """\
You are a strict judge evaluating whether a subagent's result satisfies \
a task step's acceptance criteria. You receive:

1. The step's task description (what was asked)
2. The step's validation_criteria (what constitutes success)
3. The subagent's result text (what was produced)
4. Verification evidence status (test/lint/build pass-fail if available)

Decide one of three verdicts:

PASS — the result satisfies the validation criteria:
- The deliverable was produced and matches the criteria
- OR the result explains that the task is unachievable with clear reasoning

RETRY — the result is incomplete but retryable:
- The subagent produced partial work that could be completed with another attempt
- The result contains an error that could be fixed on retry
- The validation criteria are not yet met but are achievable

BLOCK — the result indicates the step cannot be completed:
- The task is fundamentally unachievable (missing dependency, design conflict)
- The subagent exhausted its retry budget without progress
- Human intervention is required

Reply ONLY with a single JSON object on one line:
{"verdict": "pass", "reason": "<one sentence>"}
{"verdict": "retry", "reason": "<one sentence>", "feedback": "<actionable guidance for retry>"}
{"verdict": "block", "reason": "<one sentence>"}
"""

def _build_judge_prompt(
    step_task: str,
    criteria: str | None,
    result_text: str,
    evidence_summary: str | None,
) -> str:
    """Build the user prompt for the step judge."""
    parts = [f"## Step Task\n{step_task}"]
    if criteria:
        parts.append(f"## Validation Criteria\n{criteria}")
    parts.append(f"## Subagent Result\n{result_text[:8000]}")  # truncate
    if evidence_summary:
        parts.append(f"## Verification Evidence\n{evidence_summary}")
    else:
        parts.append("## Verification Evidence\n(none recorded)")
    parts.append("## Your Verdict")
    return "\n\n".join(parts)

def _parse_judge_response(raw: str) -> JudgeResult:
    """Parse the judge LLM's JSON response.

    Fail-open: any parse error → PASS.
    """
    match = re.search(
        r'\{\s*"verdict"\s*:\s*"(pass|retry|block)"',
        raw,
        re.IGNORECASE,
    )
    if not match:
        logger.warning("StepJudge: no verdict in response, failing open as PASS")
        return JudgeResult(StepVerdict.PASS, "judge parse failure (fail-open)", "")

    verdict_str = match.group(1).lower()
    verdict = StepVerdict(verdict_str)

    # Try to extract reason and feedback from JSON
    reason = ""
    feedback = ""
    try:
        # Find the full JSON object
        json_match = re.search(r'\{[^}]+\}', raw)
        if json_match:
            obj = json.loads(json_match.group(0))
            reason = obj.get("reason", "")
            feedback = obj.get("feedback", "")
    except (json.JSONDecodeError, AttributeError):
        pass

    return JudgeResult(verdict, reason, feedback)

async def judge_step_result(
    step_task: str,
    criteria: str | None,
    result_text: str,
    evidence_summary: str | None = None,
) -> JudgeResult:
    """Judge whether a subagent's step result satisfies validation criteria.

    Fail-open: any error → PASS (never block on judge failure).
    """
    if not STEP_JUDGE["enabled"]:
        return JudgeResult(StepVerdict.PASS, "step judge disabled (fail-open)", "")

    try:
        # build_auxiliary_llm 签名: (temperature: float | None = None)
        # 不接受 task= 参数；温度=0 确保确定性输出
        llm = build_auxiliary_llm(temperature=0)
        prompt = _build_judge_prompt(
            step_task, criteria,
            result_text[:STEP_JUDGE["max_result_chars"]],
            evidence_summary if STEP_JUDGE["evidence_aware"] else None,
        )

        response = await llm.ainvoke([
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])

        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_judge_response(raw)
    except Exception as e:
        logger.warning("StepJudge failed, failing open as PASS: {}", e)
        return JudgeResult(StepVerdict.PASS, f"judge error (fail-open): {e}", "")
```

#### Step 1.2：修改 `taskflow_resume.py` — 插入 StepJudge

- [ ] **修改 `agent/tools/taskflow/tools/taskflow_resume.py`**

真实入口是 `if step is not None and redispatched_key is None:` 这个块（当前文件 :146-157）：文本级失败已被上面的 `_retry.classify_failure()` 分支处理并置了 `redispatched_key`，只有走到这个 block 才轮到 StepJudge 做语义判定。

**照抄旧稿会丢状态的三个事实：**

1. 现有 resume **不调用 `mark_step_done()`，也不存在 `_ok(...)` 助手或 `rev` 变量**——它直接内联 `step["status"] = str(StepStatus.DONE)`（:148）。旧稿里的 `_ok(...)` / `rev + 1` 都是**不存在的符号**。
2. 状态持久化走真实路径：非重派分支 `store_sqlite.update_flow(flow_id, revision, session_id=…, state=state, wait=None, status=new_status, total_tokens=…, total_cost=…)`（:226-235）；重派分支 `update_flow_with_conflict_retry(...)`（:208-221）。`step` 是 `steps` 的元素，而 `state["steps"] = steps`（:159）——**先把 StepJudge 结果写回 `state["steps"]`，再调用 `update_flow`**，不要另造持久化调用。
3. RETRY 分支**复用 `_retry.py` 既有字段与助手**（不是新增 `judge_retry_count`）：`step_retry_count(step)`（:87）读 `step["retry_count"]`；`apply_redispatch(step, new_key, count)`（:170）写回 `retry_count` / `child_session_key` / `dispatched_at` / `status=DISPATCHED`；`spawn_replacement(task, requester_key)`（:161）重派（`taskflow_resume` 已导入这些）。judge 反馈写入**新增的 `step["judge_feedback"]`**（随 step 落库），由 Step 1.3 在重派时消费。

`evidence_summary` 取自 Phase 3 新建的 `agent/tools/taskflow/evidence_collector.py::collect_evidence_summary(...)`（签名见 PLAN_2 §Phase 3）。**该模块当前不存在**，先按 PLAN_2 Step 3.3 建好再接线；其 `session_key` 值用 step 的 `child_session_key`（子代理回传结果的会话键），**不是 `flow_id`，更不是不存在的 `run.session_key`**。

```python
# agent/tools/taskflow/tools/taskflow_resume.py — 只在既有 else 分支附近插入，不整函数重写

# 顶部补 import（现有文件用相对导入）：
from config.features import STEP_JUDGE
from ..step_judge import judge_step_result, StepVerdict
from ..evidence_collector import collect_evidence_summary
# 既有导入可直接用：spawn_replacement / apply_redispatch / step_retry_count /
# requester_key_for_retry（_retry）、unlock_dependents / conflict_error /
# not_found_error（_shared）、store_sqlite / update_flow_with_conflict_retry。

    # ... 现有 result_record 追加、文本级 retry 分支（可能置 redispatched_key）之后 ...

    validation_text = ""
    judge_text = ""
    if step is not None and redispatched_key is None:          # 原块 taskflow_resume.py:146-157
        criteria = (validation_criteria or "").strip()
        if criteria:
            step["validation_criteria"] = criteria
        stored_criteria = str(step.get("validation_criteria") or "").strip()

        # NEW: StepJudge —— 在内联置 DONE（:148）之前做一次 LLM 语义判定
        judge_result = await judge_step_result(
            step_task=str(step.get("task") or ""),
            criteria=stored_criteria or None,
            result_text=result,
            evidence_summary=collect_evidence_summary(
                session_key=str(step.get("child_session_key") or ""),
            ),
        )

        if (
            judge_result.verdict == StepVerdict.RETRY
            and step_retry_count(step) < STEP_JUDGE["max_retries"]
        ):
            # 复用 _retry 的重派机制：置上 redispatched_key，交给既有
            # update_flow_with_conflict_retry 分支（:208-221）持久化。
            requester_key = requester_key_for_retry(state, session_id)
            try:
                redispatched_key = await spawn_replacement(
                    str(step.get("task") or ""), requester_key
                )
            except Exception as exc:
                step["status"] = str(StepStatus.BLOCKED)
                step["block_reason"] = f"StepJudge RETRY re-dispatch failed: {exc}"
                judge_text = f"\n  judge: RETRY re-dispatch failed ({type(exc).__name__}: {exc})"
            else:
                retry_count_after = step_retry_count(step) + 1
                apply_redispatch(step, redispatched_key, retry_count_after)   # _retry.py:170
                step["judge_feedback"] = judge_result.feedback                # 写回 step，随 state 落库
                judge_text = (
                    f"\n  judge: RETRY ({retry_count_after}/{STEP_JUDGE['max_retries']}), "
                    f"child_session_key={redispatched_key}: {judge_result.reason}"
                )
        elif judge_result.verdict == StepVerdict.BLOCK:
            step["status"] = str(StepStatus.BLOCKED)
            step["block_reason"] = f"StepJudge blocked: {judge_result.reason}"
            judge_text = f"\n  judge: BLOCKED {judge_result.reason}"
        else:
            # PASS（或 RETRY 预算已耗尽 —— fail-open 视为 done）
            step["status"] = str(StepStatus.DONE)     # 原内联赋值位置 :148
            judge_text = f"\n  judge: PASS {judge_result.reason}"
        # 原「⚠ Result needs validation」警告文本被 judge 取代，validation_text 保持空串

    newly_ready = unlock_dependents(steps)
    state["steps"] = steps          # :159 —— StepJudge 结果先写回 state["steps"]
    counts = steps_summary(steps)

    # ... 现有 token 聚合（:170-186）保持不变 ...

    if redispatched_key is not None:
        # :192-221 既有分支。★ 必须在 build_state 的 conflict 重读里一并写回 judge_feedback：
        #     target = next((s for s in fresh_steps if s.get("step_id") == step_id), None)
        #     if target is not None:
        #         apply_redispatch(target, replacement_key, replacement_count)
        #         if step is not None and step.get("judge_feedback"):
        #             target["judge_feedback"] = step["judge_feedback"]
        updated, error = await update_flow_with_conflict_retry(
            flow_id, revision, flow, build_state,
            child_keys=[replacement_key], session_id=session_id,
            update_kwargs={
                "wait": None, "status": new_status,
                "total_tokens": total_tokens, "total_cost": total_cost,
            },
        )
        if updated is None:
            return error
    else:
        try:
            updated = await store_sqlite.update_flow(      # :226-235 真实持久化路径
                flow_id, revision, session_id=session_id, state=state,
                wait=None, status=new_status,
                total_tokens=total_tokens, total_cost=total_cost,
            )
        except FlowConflictError as exc:
            return conflict_error(exc)
        except FlowNotFoundError:
            return not_found_error(flow_id)

    # 返回串按现有方式拼接：validation_text + retry_text + judge_text（:243-248）
```

#### Step 1.3：修改 `taskflow_dispatch.py` — 把 judge feedback 拼进重派 task

- [ ] **修改 `agent/tools/taskflow/tools/taskflow_dispatch.py:114-121` 的派发循环**

现有循环**没有 `task_prompt` 变量**——它直接 `task=str(step.get("task") or "")` 就地展开传给 `_dispatch.dispatch_child(task=...)`（:117-118）。改动是在该调用**之前**把 `step["judge_feedback"]` 拼进 task 文本，并在成功派发后消费掉反馈。

```python
# agent/tools/taskflow/tools/taskflow_dispatch.py — 派发循环（:114-121）

    for sid in requested:
        step = by_id[sid]
        task_text = str(step.get("task") or "")
        feedback = str(step.get("judge_feedback") or "").strip()
        if feedback:
            task_text += (
                "\n\n## Previous Attempt Feedback\n"
                "The previous attempt was evaluated by a judge and needs revision:\n"
                f"{feedback}\n\n"
                "Please address this feedback and complete the task."
            )
        try:
            child_key = await _dispatch.dispatch_child(
                task=task_text,                       # :117-118 原调用点
                requester_session_key=requester_key,
                label=None,
            )
        except Exception as exc:  # tool boundary: convert to text, persist successes below
            failure = exc
            break
        if feedback:
            step.pop("judge_feedback", None)          # 已消费，随 records 落库
        # ... 现有 retry_count / status=DISPATCHED / child_session_key / dispatched_at 赋值 ...
```

> **配套修正（否则反馈会残留）**：`records.append(step)` 经 `_shared.apply_dispatched_steps`（`agent/tools/taskflow/tools/_shared.py:186-204`）落库时只回填 `status` / `child_session_key` / `dispatched_at`，不同步 `judge_feedback` 的删除。需在该函数匹配到的 `target` 上补 `target.pop("judge_feedback", None)`，否则并发写者留下的旧 step 会带着旧反馈，造成下一轮重复注入。

---

### Phase 2：子代理完成门控 — Goal Loop

#### Step 2.1：CompletionJudge 模块

- [ ] **新建 `agent/tools/subagent/spawn/completion_judge.py`**

```python
"""LLM-based completion judge for subagent runs.

Inspired by hermes-agent's goal loop (hermes_cli/goals.py:1620-1740).
After a subagent's first turn, an auxiliary LLM evaluates whether
the work is truly done. If not, a continuation prompt is injected
and the subagent runs another turn, up to a turn budget.

Differs from StepJudge (Phase 1) which judges a single step result:
- CompletionJudge judges the entire subagent run (may span multiple turns)
- CompletionJudge is called BEFORE complete_subagent_run()
- StepJudge is called AFTER resume (step result injected)
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import NamedTuple

from loguru import logger

from models import build_auxiliary_llm
from config.features import COMPLETION_JUDGE

class CompletionVerdict(StrEnum):
    DONE = "done"
    CONTINUE = "continue"

class CompletionJudgeResult(NamedTuple):
    verdict: CompletionVerdict
    reason: str
    continuation_prompt: str  # injected as user message for next turn; empty on DONE

COMPLETION_JUDGE_SYSTEM_PROMPT = """\
You are a strict judge evaluating whether an autonomous subagent has \
completed its assigned task. You receive:

1. The task description (what was asked of the subagent)
2. The subagent's most recent response (what it produced)
3. Verification evidence status (if available)

Decide one of two verdicts:

DONE — the task is fully satisfied:
- The deliverable was produced and matches the task description
- OR the response explains the task is unachievable with clear reasoning

CONTINUE — not done, and there is a concrete next step:
- The subagent produced partial work that needs more turns
- A verification step (test/build/lint) was not run
- An error occurred that could be fixed with another attempt

Reply ONLY with a single JSON object on one line:
{"verdict": "done", "reason": "<one sentence>"}
{"verdict": "continue", "reason": "<one sentence>", "continuation_prompt": "<one paragraph: what the subagent should do next>"}
"""

def _parse_completion_response(raw: str) -> CompletionJudgeResult:
    """Parse the judge LLM's JSON response.

    Fail-open: any parse error → DONE.
    """
    match = re.search(
        r'\{\s*"verdict"\s*:\s*"(done|continue)"',
        raw,
        re.IGNORECASE,
    )
    if not match:
        logger.warning("CompletionJudge: no verdict in response, failing open as DONE")
        return CompletionJudgeResult(CompletionVerdict.DONE, "judge parse failure (fail-open)", "")

    verdict_str = match.group(1).lower()
    verdict = CompletionVerdict(verdict_str)

    reason = ""
    continuation_prompt = ""
    try:
        json_match = re.search(r'\{[^}]+\}', raw)
        if json_match:
            obj = json.loads(json_match.group(0))
            reason = obj.get("reason", "")
            continuation_prompt = obj.get("continuation_prompt", "")
    except (json.JSONDecodeError, AttributeError):
        pass

    return CompletionJudgeResult(verdict, reason, continuation_prompt)

async def judge_completion(
    task_text: str,
    last_response: str,
    evidence_summary: str | None = None,
) -> CompletionJudgeResult:
    """Judge whether a subagent run is complete.

    Fail-open: any error → DONE (never trap a subagent in a goal loop
    on judge failure).
    """
    if not COMPLETION_JUDGE["enabled"]:
        return CompletionJudgeResult(CompletionVerdict.DONE, "completion judge disabled (fail-open)", "")

    try:
        llm = build_auxiliary_llm(temperature=0)
        parts = [f"## Task\n{task_text}"]
        parts.append(f"## Subagent Response\n{last_response[:8000]}")
        if evidence_summary:
            parts.append(f"## Verification Evidence\n{evidence_summary}")
        else:
            parts.append("## Verification Evidence\n(none recorded)")
        parts.append("## Your Verdict")

        response = await llm.ainvoke([
            {"role": "system", "content": COMPLETION_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ])

        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_completion_response(raw)
    except Exception as e:
        logger.warning("CompletionJudge failed, failing open as DONE: {}", e)
        return CompletionJudgeResult(CompletionVerdict.DONE, f"judge error (fail-open): {e}", "")
```

#### Step 2.2：修改 `spawn/core.py` — Goal Loop

- [ ] **修改 `_execute_subagent()` 插入 goal loop**

**实际签名**（`agent/tools/subagent/spawn/core.py:533-542`）：`_execute_subagent(run, system_prompt, user_message, forked_messages, tools, timeout_seconds, model_override=None, output_schema=None) -> None`

**照抄旧稿会错的真实调用约定：**

- 子 agent 的实际调用是 `child_agent.ainvoke(input={"session_id": run.child_session_key, "messages": messages}, config=agent_config)`（:626-636）。**没有 `run.session_key`**，会话键是 `run.child_session_key`。
- `agent_config` 由 `build_agent_config(session_id=run.child_session_key)`（`pub/func/build_agent_config.py:6-7`，经 `pub.func` 导出）生成，thread_id 为 `rand_str_to_int(child_session_key)`。每轮 continuation 复用**同一个** config，即可借 checkpointer 延续同一会话。
- 结果文本用 `_extract_result_text(agent_result)`（`spawn/core.py:66-114`）提取，**不要**自己 `aget_state()` 扫描消息。
- `RunOutcome` 只有 `status` + `error`（**无 `reason`**）；预算耗尽用 `error="goal_loop_budget_exhausted"`。
- 局部 `outcome` 初值 `RunOutcome(status=RunOutcomeStatus.OK)`（:583），异常映射在 :659-666，`finally` 里统一 `await complete_subagent_run(run.run_id, outcome, result_text, expected_generation=run.generation)`（:733-737）。Goal loop 只改这个局部 `outcome` 与 `result_text`。

```python
# agent/tools/subagent/spawn/core.py — _execute_subagent() 的 goal-loop 插入（示意，非整函数重写）

from langchain_core.messages import HumanMessage
from pub.func import build_agent_config

async def _execute_subagent(
    run, system_prompt, user_message, forked_messages, tools,
    timeout_seconds, model_override=None, output_schema=None,
    *, goal_loop: bool = False, goal_max_turns: int = 5,   # NEW
) -> None:
    # ... effective_deny 计算（现有 :568-579）...

    result_text: str | None = None
    outcome = RunOutcome(status=RunOutcomeStatus.OK)        # 现有 :583

    try:
        child_agent = await _build_child_agent(             # 现有 :587-594
            system_prompt=system_prompt, tools=tools,
            tool_allow=run.inherited_tool_allow, tool_deny=effective_deny,
            role=run.role, model_override=model_override,
        )
        messages = list(forked_messages)                    # 现有 :597-598
        messages.append(HumanMessage(content=user_message))
        agent_config = build_agent_config(session_id=run.child_session_key)   # 现有 :601-609
        if run.thinking:
            agent_config["tags"] = agent_config.get("tags", [])
            agent_config["tags"].append(f"thinking:{run.thinking}")
        if run.spawned_cwd:
            agent_config["cwd"] = run.spawned_cwd

        async def _invoke(msgs: list) -> dict:
            payload = {"session_id": run.child_session_key, "messages": msgs}
            if timeout_seconds > 0:
                return await asyncio.wait_for(
                    child_agent.ainvoke(input=payload, config=agent_config),
                    timeout=timeout_seconds,
                )
            return await child_agent.ainvoke(input=payload, config=agent_config)

        agent_result = await _invoke(messages)               # 现有 :624-636
        result_text = _extract_result_text(agent_result)     # 现有 :643

        if goal_loop and goal_max_turns > 1:
            from .completion_judge import judge_completion, CompletionVerdict   # 同目录
            from agent.tools.taskflow.evidence_collector import collect_evidence_summary

            turns_used = 1
            while turns_used < goal_max_turns:
                judge_result = await judge_completion(
                    task_text=user_message,
                    last_response=result_text or "",
                    evidence_summary=collect_evidence_summary(
                        session_key=run.child_session_key,
                    ),
                )
                if judge_result.verdict == CompletionVerdict.DONE:
                    break
                agent_result = await _invoke(
                    [HumanMessage(content=judge_result.continuation_prompt)]
                )
                result_text = _extract_result_text(agent_result)
                turns_used += 1
            else:
                outcome = RunOutcome(
                    status=RunOutcomeStatus.OK,
                    error="goal_loop_budget_exhausted",
                )

        # 现有 output_schema 校验（:646-653）保持不动

    except TimeoutError:                                    # 现有 :659-666 异常映射
        outcome = RunOutcome(
            status=RunOutcomeStatus.TIMEOUT,
            error=f"Subagent timed out after {timeout_seconds}s",
        )
    except asyncio.CancelledError:
        outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Subagent was killed")
    except Exception as e:
        outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))
    finally:
        # ... 现有 OutputRepetitionGuard / remove_task / thread unbind / grace 逻辑 ...
        await complete_subagent_run(                         # 现有 :733-737 统一收口
            run.run_id, outcome, result_text, expected_generation=run.generation
        )
```

> `RunOutcome` 的字段名以 `agent/tools/subagent/types/registry.py` 的定义为准（当前为 `status` + `error`）。

#### Step 2.3：修改 `spawn_subagent_direct()` — 暴露 goal_loop 参数

- [ ] **在 `agent/tools/subagent/spawn/core.py::spawn_subagent_direct()`（:161-181）新增可选参数并透传**：

```python
async def spawn_subagent_direct(
    task: str,
    requester_session_key: str,
    # ... 现有参数不变（agent_id / task_name / label / thinking / spawn_mode /
    #     cleanup / context / attachments / cwd / requester_session_id /
    #     completion_owner_key / expects_completion_message / run_timeout_seconds /
    #     swarm_group_id / output_schema / model / launch_fingerprint） ...
    goal_loop: bool = False,       # NEW
    goal_max_turns: int = 5,       # NEW
) -> SpawnResult:
    # ... 现有逻辑 ...
    # 在 asyncio.create_task(...) 派发 _execute_subagent_with_lane(...) 的参数里
    # 透传 goal_loop=goal_loop, goal_max_turns=goal_max_turns，最终到达 _execute_subagent()。
```

- [ ] **修改 `sessions_spawn` 工具 schema 与调用链**（schema 归属：`agent/tools/subagent/tools/sessions_spawn.py`，**不是** `agent/tools/__init__.py`）：
  - `SessionsSpawnSchema`（:21）新增 `goal_loop: bool = False`、`goal_max_turns: int = 5`；
  - `SessionsSpawnTool._arun`（:70）新增同名形参；
  - 在 `spawn_subagent_direct(...)` 调用（:102-115）里透传 `goal_loop=goal_loop, goal_max_turns=goal_max_turns`。

---

## 5. 已存在、勿重建（执行前必读）

以下组件/助手**已在代码库中实现**，本计划只做接线，禁止重造：

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| `mark_step_done(steps, child_session_key)` | `agent/tools/taskflow/tools/_shared.py:138` | 幂等标记 step 为 done（当前 resume **未**用它，走内联赋值） |
| `unlock_dependents(steps)` | `agent/tools/taskflow/tools/_shared.py:153` | 单趟解锁 blocked→ready |
| `_retry` 全套 | `agent/tools/taskflow/tools/_retry.py` | `classify_failure` :56、`normalize_policy` :67、`step_retry_count` :87、`apply_redispatch` :170、`exhausted_note` :178 |
| `build_auxiliary_llm(temperature=...)` | `models/LLMs/auxiliary_llm/core.py:48` | **无 `task=` 参数**，勿传 task |
| `EvidenceLedger` | `agent/tools/todolist/evidence_ledger.py:18` | append-only；`LEDGER_PATH` 由 `config.path.resolve_evidence_ledger_path()`（`config/path.py:212`）解析 |
| `SisyphusVerifier` | `agent/tools/todolist/verifier.py:218` | `verify()` 是 `@staticmethod async`，签名 `(session_id, todo, plan_path, checkbox_label)` |
| `_VERIFICATION_REMINDER` | `agent/middlewares/subagent_completion_drain/core.py:42` | 现有提示文本，勿重写 |
| taskflow `session_id` 列 | `agent/tools/taskflow/registry/store_sqlite.py:69`；迁移先例 `_SESSION_ID_COLUMN_DDL` :107 | flow 已有 session 隔离列，勿再加 |
| `tool_error(...)` | `agent/tools/pub_base/tool_utils.py:6` | 返回 JSON 字符串；**taskflow 家族不用它**，一律直接 `return "Error: ..."` |
| `build_agent_config(session_id=...)` | `pub/func/build_agent_config.py:6`（经 `pub.func` 导出） | 子代理 ainvoke 的 config 构造器 |

---

> **续接**：Phase 3-4 实现计划及配置/测试/回滚等章节见 [STEP_JUDGE_PLAN_2.md](./STEP_JUDGE_PLAN_2.md)
