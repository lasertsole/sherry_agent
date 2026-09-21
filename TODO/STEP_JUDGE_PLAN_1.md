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
| 验证证据跟踪        | `EvidenceLedger` + `SisyphusVerifier` 5-gate **存在但仅在测试中调用**            | `verification_evidence.py` 生产路径自动记录 | 存在但未接入 |
| 完成门控            | `_assert_transition_allowed()` 检查存活/状态，**不检查质量**                     | `kanban_complete` 调 `judge_goal` 门控      | 无质量门控   |
| validation_criteria | 存储在 step 上，**仅回显为警告文本** "⚠ Result needs validation"                 | `GoalContract` 传入 judge prompt 强制评估   | 存在但未强制 |
| Goal Loop           | 无。子代理一次性执行，返回即完成                                                 | 循续轮驱动直到 done 或预算耗尽              | 完全缺失     |
| 提示级提醒          | `SubagentCompletionDrainMiddleware` 附加 `_VERIFICATION_REMINDER` 文本           | 无（用程序门控代替提示）                    | 提示 ≠ 强制  |

**核心问题**：Sherry 的完成验证策略是**提示驱动的编排**——SKILL.md 告诉 LLM 遵循 Sisyphus 合约，`SubagentCompletionDrainMiddleware` 附加文本提醒，但代码级强制仅限于存活/状态检查。SisyphusVerifier 有完整 5-gate 实现但未接入生产调用链。

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
                    │    PASS ──► mark_step_done() ──► unlock_deps()   │
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
| StepJudge             | `taskflow_resume.py` — 在内联设置 `step["status"]=DONE` 之前       | 插入 judge 调用               |
| CompletionJudge       | `spawn/core.py::_execute_subagent()` — 在 `run.outcome` 赋值之前   | 插入 goal loop                |
| SisyphusVerifier 接入 | `taskflow_finish.py` — 在状态转换为 DONE 之前                      | 插入适配层门控                |
| Evidence Ledger 接入  | `terminal.py` / `python_repl.py` 工具执行后                        | 自动记录 exit code            |
| Evidence stale 标记   | `file_tools/write_file.py` / `file_tools/patch_file.py` 工具执行后 | 标记关联 evidence 为 stale    |
| Completion Drain 升级 | `subagent_completion_drain/core.py` — `abefore_model` hook         | 类属性 `enforce_verification` |

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

- [ ] **修改 `taskflow_resume` 工具**

在步骤标记 DONE 之前插入 judge 调用。注意：现有 `taskflow_resume` **直接内联设置** `step["status"] = str(StepStatus.DONE)`，不调用 `mark_step_done()`。StepJudge 插在**内联设置之前**，替换原有逻辑。

**与现有 `_retry.py` 的关系**：当前 `taskflow_resume` 在 result 被判定为失败时（`classify_failure()` 文本子串匹配），通过 retry_policy 重新 dispatch。StepJudge 是**额外的语义层判定**——在 `_retry.py` 的文本模式判定通过后（即 result 不含 timeout/rate_limit/error 关键词），StepJudge 再做一次 LLM 语义评估。两层叠加：文本层过滤明显失败 → LLM 层评估语义完成度。

```python
# taskflow_resume.py — 修改后的 resume 逻辑
# 现有代码先做 _retry.classify_failure 判定（文本模式），通过后才到 StepJudge

from agent.tools.taskflow.step_judge import judge_step_result, StepVerdict
from agent.tools.taskflow.evidence_collector import collect_evidence_summary

# ... 现有 retry 逻辑结束后（result 不触发 retry）...

# NEW: StepJudge — 在标记 step done 之前评估结果
step = steps[idx]
step_task = step.get("task", "")
step_criteria = validation_criteria or step.get("validation_criteria")
evidence_summary = collect_evidence_summary(flow_id=flow_id, step_id=step["step_id"])

judge_result = await judge_step_result(
    step_task=step_task,
    criteria=step_criteria,
    result_text=result,
    evidence_summary=evidence_summary,
)

if judge_result.verdict == StepVerdict.PASS:
    # 原有逻辑：内联标记 done + unlock_dependents（替换原来的直接赋值）
    step["status"] = str(StepStatus.DONE)
    # 移除原有的 "⚠ Result needs validation" 警告文本——judge 已强制评估
    newly_ready = unlock_dependents(steps)

elif judge_result.verdict == StepVerdict.RETRY:
    # StepJudge 判定重试——与 _retry.py 的 retry 机制叠加
    retry_count = step.get("retry_count", 0)
    # StepJudge 重试使用独立预算，不与 _retry.py 的 retry_policy 共用计数
    judge_max_retries = STEP_JUDGE["max_retries"]
    total_retries = step.get("judge_retry_count", 0)

    if total_retries < judge_max_retries:
        step["judge_retry_count"] = total_retries + 1
        step["judge_feedback"] = judge_result.feedback
        step["status"] = str(StepStatus.READY)  # 重新进入待派发
        # 注意：不标记 DONE，不 unlock dependents
        return _ok(
            f"StepJudge RETRY ({total_retries + 1}/{judge_max_retries}): "
            f"{judge_result.reason}\nFeedback: {judge_result.feedback}",
            revision=rev + 1,
        )
    else:
        # StepJudge 重试预算耗尽 → 标记 blocked
        step["status"] = str(StepStatus.BLOCKED)
        step["block_reason"] = f"StepJudge retry exhausted: {judge_result.reason}"
        return _ok(
            f"StepJudge BLOCKED (retry exhausted): {judge_result.reason}",
            revision=rev + 1,
        )

elif judge_result.verdict == StepVerdict.BLOCK:
    step["status"] = str(StepStatus.BLOCKED)
    step["block_reason"] = f"StepJudge blocked: {judge_result.reason}"
    return _ok(
        f"StepJudge BLOCKED: {judge_result.reason}",
        revision=rev + 1,
    )
```

#### Step 1.3：修改 `taskflow_dispatch.py` — 传递 judge feedback

- [ ] **修改 dispatch 逻辑**，在 re-dispatch 时将 judge feedback 注入子代理 prompt

```python
# taskflow_dispatch.py — re-dispatch 时注入 judge feedback

if step.get("judge_feedback"):
    task_prompt += (
        f"\n\n## Previous Attempt Feedback\n"
        f"The previous attempt was evaluated by a judge and needs revision:\n"
        f"{step['judge_feedback']}\n\n"
        f"Please address this feedback and complete the task."
    )
    # 清除 feedback 避免下次重复
    step.pop("judge_feedback", None)
```

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

**实际签名**：`_execute_subagent(run: SubagentRunRecord, system_prompt: str, user_message: str, forked_messages: list, tools: list | None, timeout_seconds: float, model_override: str | None = None, output_schema: dict | None = None) -> None`

`RunOutcome` 是 Pydantic BaseModel：`status: RunOutcomeStatus` + `error: str | None`（**无 `reason` 字段**）。

**LangGraph ainvoke 二次调用问题**：LangGraph 的 `create_agent` 创建的 agent 有 checkpointer，`ainvoke()` 调用后状态会持久化。Goal loop 不能简单调用两次 `ainvoke()`——第二次调用需要传入新的 HumanMessage 作为后续轮次。利用 checkpointer 的 thread_id 自动延续对话历史。

```python
# spawn/core.py — _execute_subagent() 修改

# RunOutcome 只有 status + error，无 reason 字段
# 预算耗尽时用 error 字段携带原因
from agent.tools.subagent.types.registry import RunOutcome, RunOutcomeStatus

async def _execute_subagent(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    forked_messages: list,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    output_schema: dict | None = None,
    *,
    goal_loop: bool = False,       # NEW
    goal_max_turns: int = 5,       # NEW
) -> None:
    """Execute subagent with optional goal loop.

    goal_loop=True: after each turn, CompletionJudge evaluates.
      - DONE → complete
      - CONTINUE → inject continuation prompt as new HumanMessage, invoke again
      - budget exhausted → complete with error="goal_loop_budget_exhausted"
    goal_loop=False (default): single-shot execution (current behavior)
    """
    # ... 构建 child_agent（现有逻辑） ...

    if not goal_loop:
        # 原有逻辑：单次 ainvoke
        result = await child_agent.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config={"configurable": {"thread_id": run.session_key}},
        )
        run.outcome = RunOutcome(status=RunOutcomeStatus.OK)
        return

    # Goal loop 模式
    from agent.tools.subagent.spawn.completion_judge import (
        judge_completion, CompletionVerdict,
    )
    from agent.tools.taskflow.evidence_collector import collect_evidence_summary

    turns_used = 0
    thread_config = {"configurable": {"thread_id": run.session_key}}

    # 第一轮
    await child_agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=thread_config,
    )
    turns_used += 1

    while turns_used < goal_max_turns:
        # 提取最新响应
        state = await child_agent.aget_state(config=thread_config)
        messages = state.values.get("messages", [])
        last_response = ""
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.type != "tool":
                last_response = str(msg.content)
                break

        # Judge 评估
        evidence = collect_evidence_summary(session_key=run.session_key)
        judge_result = await judge_completion(
            task_text=user_message,
            last_response=last_response,
            evidence_summary=evidence,
        )

        if judge_result.verdict == CompletionVerdict.DONE:
            run.outcome = RunOutcome(status=RunOutcomeStatus.OK)
            return

        # CONTINUE: 注入 continuation prompt 作为新 HumanMessage
        await child_agent.ainvoke(
            {"messages": [{"role": "user", "content": judge_result.continuation_prompt}]},
            config=thread_config,
        )
        turns_used += 1

    # 预算耗尽
    logger.warning(
        "Subagent goal loop exhausted: {} turns, task: {}",
        turns_used, user_message[:200],
    )
    run.outcome = RunOutcome(
        status=RunOutcomeStatus.OK,
        error="goal_loop_budget_exhausted",
    )
```

#### Step 2.3：修改 `spawn_subagent_direct()` — 暴露 goal_loop 参数

- [ ] **修改 `spawn_subagent_direct()` 签名**

```python
async def spawn_subagent_direct(
    ...,
    goal_loop: bool = False,  # NEW
    goal_max_turns: int = 5,  # NEW
) -> SpawnResult:
```

- [ ] **修改 `sessions_spawn` 工具 schema**，暴露 `goal_loop` / `goal_max_turns` 参数

---

> **续接**：Phase 3-4 实现计划及配置/测试/回滚等章节见 [STEP_JUDGE_PLAN_2.md](./STEP_JUDGE_PLAN_2.md)
