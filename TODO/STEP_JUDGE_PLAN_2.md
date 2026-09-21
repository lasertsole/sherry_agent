# TaskFlow Step Judge & 子代理完成门控计划（下）

> 本文接续 [STEP_JUDGE_PLAN_1.md](./STEP_JUDGE_PLAN_1.md)，包含 Phase 3-4 实现计划、配置设计、测试计划、实施顺序、回滚方案、文件变更清单及与 hermes-agent 的关键差异。

## 4. 实现计划（续）

### Phase 3：Evidence Ledger 接入生产

#### Step 3.0：扩展 EvidenceLedger 类（添加缺失方法）

- [ ] **修改 `agent/tools/todolist/evidence_ledger.py`**，添加计划所需的方法

当前 `EvidenceLedger` 是全局单例（单 JSONL 文件），只有 `append(entry: dict)` 和 `read_all() -> list[dict]`。需要扩展为支持 session 隔离和 stale 标记：

```python
# evidence_ledger.py — 扩展（在现有 append/read_all 基础上新增）

class EvidenceLedger:
    LEDGER_PATH = str(resolve_evidence_ledger_path())

    # --- 现有方法（不变） ---
    @classmethod
    def append(cls, entry: dict) -> None: ...
    @classmethod
    def read_all(cls) -> list[dict]: ...

    # --- 新增方法 ---
    @classmethod
    def for_session(cls, session_key: str) -> "SessionEvidenceLedger":
        """返回 session 隔离的 evidence ledger 视图。

        底层仍共享同一个 JSONL 文件，但查询时按 session_id 过滤。
        """
        return SessionEvidenceLedger(session_key)

    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]:
        """读取指定 session 的所有 evidence 记录。"""
        return [
            r for r in cls.read_all()
            if r.get("session_id") == session_key
        ]

    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int:
        """标记与指定文件路径相关的 evidence 为 stale。

        Returns: 标记的记录数。
        """
        count = 0
        records = cls.read_all()
        for r in records:
            if session_key and r.get("session_id") != session_key:
                continue
            # 匹配逻辑：evidence 记录中 commands 列表包含该路径
            commands = r.get("commands", [])
            if any(file_path in str(c) for c in commands):
                r["is_stale"] = True
                count += 1
        # 写回（简化：全量重写）
        # 实际实现应考虑并发安全
        return count


class SessionEvidenceLedger:
    """Session-scoped evidence ledger view."""

    def __init__(self, session_key: str):
        self.session_key = session_key

    def append(self, **entry_fields) -> None:
        """追加 evidence 记录，自动注入 session_id。"""
        entry_fields["session_id"] = self.session_key
        EvidenceLedger.append(entry_fields)

    def list_records(self) -> list[dict]:
        """列出当前 session 的所有 evidence。"""
        return EvidenceLedger.read_for_session(self.session_key)

    def mark_stale_for_path(self, file_path: str) -> int:
        return EvidenceLedger.mark_stale_for_path(file_path, self.session_key)
```

#### Step 3.1：Terminal/PythonREPL 工具结果自动记录

- [ ] **修改 `agent/tools/terminal.py` 和 `agent/tools/python_repl.py`**，在工具执行后自动记录验证证据

```python
# agent/tools/terminal.py — 在 SafeShellTool 执行后自动记录
# 实际接入点：通过中间件 wrap_tool_call 或在 build_terminal_tool() 包装

from agent.tools.todolist.evidence_ledger import EvidenceLedger
from config.features import EVIDENCE_LEDGER

# 可配置的验证命令分类（从 config 读取，非硬编码）
_DEFAULT_VERIFY_COMMANDS = {
    "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
    "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
    "build": ["cargo build", "npm run build", "make", "cmake"],
    "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
    "format": ["ruff format", "prettier", "black"],
}

def _classify_command(command: str) -> str | None:
    """Classify a terminal command as verification evidence."""
    cmd_lower = command.lower().strip()
    for kind, patterns in _DEFAULT_VERIFY_COMMANDS.items():
        if any(p in cmd_lower for p in patterns):
            return kind
    return None

def _record_evidence(command: str, exit_code: int, output: str, session_id: str):
    """在 terminal 工具执行后调用。"""
    if not EVIDENCE_LEDGER["auto_record"]:
        return
    kind = _classify_command(command)
    if kind:
        ledger = EvidenceLedger.for_session(session_id)
        ledger.append(
            kind=kind,
            command=command,
            exit_code=exit_code,
            status="passed" if exit_code == 0 else "failed",
            output_snippet=output[:500],
        )
```

#### Step 3.2：文件编辑后标记 evidence stale

- [ ] **修改 `agent/tools/file_tools/` 包中的 `write_file.py` / `patch_file.py` 工具**，在文件修改后标记关联 evidence 为 stale

```python
# 在 file_tools/write_file.py 和 file_tools/patch_file.py 的工具执行后:
from agent.tools.todolist.evidence_ledger import EvidenceLedger
from config.features import EVIDENCE_LEDGER

def _mark_evidence_stale(file_path: str, session_id: str):
    if not EVIDENCE_LEDGER["auto_stale"]:
        return
    ledger = EvidenceLedger.for_session(session_id)
    ledger.mark_stale_for_path(file_path)
```

#### Step 3.3：Evidence summary 供 Judge 使用

- [ ] **新建 `agent/tools/taskflow/evidence_collector.py`**

```python
"""Collect verification evidence summary for judge prompts."""

from agent.tools.todolist.evidence_ledger import EvidenceLedger

def collect_evidence_summary(
    flow_id: str | None = None,
    step_id: str | None = None,
    session_key: str | None = None,
) -> str | None:
    """Build a human-readable evidence summary for the judge prompt.

    Returns None if no evidence recorded.
    Uses EvidenceLedger.for_session() (Phase 3 扩展) or falls back to
    read_all() filtered by session_id in entry dict.
    """
    sid = session_key or flow_id or "default"
    try:
        ledger = EvidenceLedger.for_session(sid)
        records = ledger.list_records()
    except AttributeError:
        # Phase 3 尚未实施——for_session 不存在时回退到全局 read_all
        records = [
            r for r in EvidenceLedger.read_all()
            if r.get("session_id") == sid
        ]

    if not records:
        return None

    lines = []
    for r in records:
        status = r.get("status", "unknown")
        icon = "pass" if status == "passed" else "FAIL" if status == "failed" else "?"
        stale_tag = " [stale]" if r.get("is_stale") else ""
        kind = r.get("kind", "unknown")
        cmd = r.get("command", "")
        lines.append(f"  [{icon}] {kind}: `{cmd}`{stale_tag}")

    return "\n".join(lines)
```

---

### Phase 4：Finish Gate — SisyphusVerifier 接入生产

#### Step 4.1：修改 `taskflow_finish.py` — 插入 SisyphusVerifier 前置门控

- [ ] **修改 `taskflow_finish` 工具**，在标记 DONE 之前调用 SisyphusVerifier

**实际签名**：`SisyphusVerifier.verify()` 是 `@staticmethod async`，签名为 `(session_id: str, todo: dict, plan_path: str, checkbox_label: str) -> tuple[bool, dict]`。它面向 **todolist 系统**，不直接接受 taskflow 的 steps/flow_state。本计划需要在 SisyphusVerifier 与 taskflow 之间增加一个适配层。

```python
# taskflow_finish.py — 修改后的 finish 逻辑

# SisyphusVerifier.verify() 返回 tuple[bool, dict]，不是带属性的对象
# 签名是 (session_id, todo, plan_path, checkbox_label) — 面向 todolist
# taskflow 没有对应的 todo/plan_path，需要适配
from agent.tools.todolist.verifier import SisyphusVerifier

async def taskflow_finish(flow_id, *, summary, expected_revision, session_id):
    # ... 现有逻辑 ...

    # NEW: Finish gate
    # Gate A: 所有步骤必须 done 或 blocked（DAG 完整性检查）
    steps = flow_state["steps"]
    pending = [
        s for s in steps
        if s["status"] not in (StepStatus.DONE.value, StepStatus.BLOCKED.value)
    ]
    if pending:
        return tool_error(
            f"Cannot finish: {len(pending)} step(s) not done/blocked: "
            f"{[s['step_id'] for s in pending]}"
        )

    # Gate B: 有 blocked 步骤 → 拒绝 finish（需人工介入）
    blocked = [s for s in steps if s["status"] == StepStatus.BLOCKED.value]
    if blocked:
        return tool_error(
            f"Cannot finish: {len(blocked)} step(s) are blocked: "
            f"{[s.get('block_reason', 'unknown') for s in blocked]}"
        )

    # Gate C: Evidence gate — 检查是否有 failed/stale evidence
    from agent.tools.taskflow.evidence_collector import collect_evidence_summary
    evidence = collect_evidence_summary(flow_id=flow_id)
    if evidence and "FAIL" in evidence:
        return tool_error(
            f"Finish gate rejected: failing verification evidence:\n{evidence}"
        )
    if evidence and "[stale]" in evidence:
        return tool_error(
            f"Finish gate rejected: stale evidence (code changed since "
            f"last verification):\n{evidence}\n"
            f"Re-run verification commands before finishing."
        )

    # Gate D: SisyphusVerifier — 如果 flow 关联了 todolist
    # SisyphusVerifier 面向 todolist，只在 flow 有关联 todo 时调用
    linked_todo = flow_state.get("linked_todo")
    plan_path = flow_state.get("plan_path")
    if linked_todo and plan_path:
        passed, verify_evidence = await SisyphusVerifier.verify(
            session_id=session_id,
            todo=linked_todo,
            plan_path=plan_path,
            checkbox_label=flow_state.get("todo_label", ""),
        )
        if not passed:
            reason = verify_evidence.get("reason", "unknown")
            return tool_error(
                f"Finish gate rejected by SisyphusVerifier: {reason}"
            )

    # 全部通过 → 标记 DONE
    updated = await store_sqlite.update_flow(
        flow_id, revision, ..., status=TaskFlowStatus.DONE.value
    )
```

#### Step 4.2：修改 `SubagentCompletionDrainMiddleware` — 升级为程序门控

- [ ] **修改 `SubagentCompletionDrainMiddleware`**

**实际结构**：继承 `AgentMiddleware`，**无自定义 `__init__`**。实现 `abefore_model` / `before_model`（**不是 `after_model`**）。在 `agent/core.py` 和 `spawn/core.py` 中无参实例化。

从文本提醒升级为**可选的程序门控**：

```python
# subagent_completion_drain/core.py — 修改后

from config.features import EVIDENCE_LEDGER

class SubagentCompletionDrainMiddleware(AgentMiddleware):
    # 新增配置——通过类属性而非 __init__（保持无参实例化兼容）
    enforce_verification: bool = False  # 默认 False = 文本提醒（当前行为）

    async def abefore_model(self, state, runtime=None):
        # ... 现有 drain 逻辑（排空 steering queue）...

        if not self.enforce_verification:
            # 文本提醒（当前行为）——在 drain 出的消息后附加
            # 注意：现有代码在 before_model 中操作 messages
            return {"messages": drained + [_VERIFICATION_REMINDER_MSG]}
            # 或通过 runtime 注入，取决于现有实现

        # 程序门控模式——检查 evidence ledger
        from agent.tools.taskflow.evidence_collector import collect_evidence_summary
        session_key = _extract_session_key(state)
        evidence = collect_evidence_summary(session_key=session_key)
        if not evidence or "FAIL" in evidence:
            # 有未通过的验证 → 注入强制验证 prompt
            gate_msg = HumanMessage(content=(
                "[GATE] Completion blocked: verification evidence "
                "missing or failing. Run verification commands "
                "(test/lint/build) before completing."
            ))
            return {"messages": drained + [gate_msg]}
        return {"messages": drained}
```

**接线方式**：`enforce_verification` 作为类属性，在 `agent/core.py` 实例化后设置：

```python
# agent/core.py — built_agent() 中
drain_mw = SubagentCompletionDrainMiddleware()
drain_mw.enforce_verification = EVIDENCE_LEDGER.get("enforce_on_complete", False)
# 或从新的配置项读取
```

---

## 5. 与现有计划的依赖关系

```
subagent-role-migration.md
  │
  ├─ Phase 1（功能角色）── 无依赖，可独立实施
  │
  └─ Phase 2（质量门禁: verify step + reviewer 角色）
       │
       │  本计划的 Phase 1 StepJudge 可替代 Phase 2 的
       │  _parse_verify_verdict() + reviewer subagent 方案：
       │  - Phase 2 用 reviewer subagent 做 judge（子代理判别子代理）
       │  - 本计划用 auxiliary LLM 做 judge（独立模型判别）
       │  两者可共存：verify step 用 reviewer subagent，
       │  普通 step 用 StepJudge（auxiliary LLM）
       │
       └─ 本计划 Phase 1-4 可在 Phase 2 之后或独立实施

DEEPAGENTS_BORROWING_PLAN.md P1-6（中间件脚手架保护）
  │
  └─ 无依赖。如果本计划新增了中间件（如 SubagentCompletionDrainMiddleware 升级），
     需要同步更新 _MAIN_REQUIRED / _SUBAGENT_REQUIRED
```

---

## 6. 配置设计

- [ ] **新建 `config/features/agent_side/step_judge.py`**

```python
from typing import TypedDict

class StepJudgeConfig(TypedDict):
    enabled: bool
    """全局开关。False 时所有 judge 调用跳过（fail-open 到 PASS）。"""
    max_retries: int
    """RETRY verdict 的最大重试次数。默认 2。"""
    llm_task: str
    """auxiliary LLM 的 task 标识。默认 "step_judge"。"""
    max_result_chars: int
    """传给 judge 的 result_text 截断长度。默认 8000。"""
    evidence_aware: bool
    """是否在 judge prompt 中包含 evidence summary。默认 True。"""

STEP_JUDGE = StepJudgeConfig(
    enabled=True,
    max_retries=2,
    llm_task="step_judge",
    max_result_chars=8000,
    evidence_aware=True,
)
```

- [ ] **新建 `config/features/agent_side/completion_judge.py`**

```python
from typing import TypedDict

class CompletionJudgeConfig(TypedDict):
    enabled: bool
    """全局开关。False 时 goal loop 禁用，子代理单次执行。"""
    max_turns: int
    """goal loop 最大轮次。默认 5。"""
    llm_task: str
    """auxiliary LLM 的 task 标识。默认 "completion_judge"。"""
    fail_open_verdict: str
    """judge 失败时的默认 verdict。默认 "done"（fail-open）。"""

COMPLETION_JUDGE = CompletionJudgeConfig(
    enabled=False,  # 默认关闭，需要显式开启
    max_turns=5,
    llm_task="completion_judge",
    fail_open_verdict="done",
)
```

- [ ] **新建 `config/features/agent_side/evidence_ledger.py`**

```python
from typing import TypedDict

class EvidenceLedgerConfig(TypedDict):
    auto_record: bool
    """是否自动记录 terminal/python_repl 的验证命令结果。默认 True。"""
    auto_stale: bool
    """是否在文件编辑后自动标记 evidence stale。默认 True。"""
    ledger_path: str
    """JSONL 文件路径。默认 "data/evidence-ledger.jsonl"。"""

EVIDENCE_LEDGER = EvidenceLedgerConfig(
    auto_record=True,
    auto_stale=True,
    ledger_path="data/evidence-ledger.jsonl",
)
```

---

## 7. 测试计划

### Phase 1 测试

- [ ] **T1.1** — `tests/agent/tools/taskflow/test_step_judge.py`
  - `judge_step_result()` 对 PASS/RETRY/BLOCK 三种结果正确解析
  - fail-open：LLM 不可用时返回 PASS
  - fail-open：LLM 返回乱码时返回 PASS
  - evidence_summary 为 None 时正常工作
  - result_text 超过 max_result_chars 时正确截断

- [ ] **T1.2** — `tests/agent/tools/taskflow/test_resume_with_judge.py`
  - PASS verdict → step marked done, dependents unlocked
  - RETRY verdict (retry_count < max) → step status=READY, judge_feedback stored
  - RETRY verdict (retry_count >= max) → step BLOCKED, block_reason set
  - BLOCK verdict → step BLOCKED
  - judge 不可用 → fail-open PASS, step marked done
  - re-dispatch 时 judge_feedback 注入 prompt

### Phase 2 测试

- [ ] **T2.1** — `tests/agent/tools/subagent/test_completion_judge.py`
  - `judge_completion()` DONE/CONTINUE 正确解析
  - fail-open：LLM 不可用 → DONE
  - continuation_prompt 非空 on CONTINUE

- [ ] **T2.2** — `tests/agent/tools/subagent/test_goal_loop.py`
  - goal_loop=False → 单次执行（当前行为）
  - goal_loop=True, judge DONE on turn 1 → 完成，turns_used=1
  - goal_loop=True, judge CONTINUE on turn 1, DONE on turn 2 → 完成，turns_used=2
  - goal_loop=True, budget exhausted → outcome="goal_loop_budget_exhausted"
  - continuation prompt 正确注入为 HumanMessage

### Phase 3 测试

- [ ] **T3.1** — `tests/agent/tools/test_evidence_auto_record.py`
  - terminal 运行 `pytest` → evidence 记录为 kind="test"
  - terminal 运行 `ruff check .` → evidence 记录为 kind="lint"
  - terminal 运行 `ls` → 不记录
  - exit code 0 → status="passed"
  - exit code non-0 → status="failed"
  - python_repl 运行测试代码 → 记录为 kind="test"

- [ ] **T3.2** — `tests/agent/tools/test_evidence_stale.py`
  - write_file 修改 foo.py → foo.py 相关 evidence 标记 stale
  - patch_file 修改 bar.py → bar.py 相关 evidence 标记 stale
  - stale evidence 在 judge prompt 中显示 [stale] 标签

### Phase 4 测试

- [ ] **T4.1** — `tests/agent/tools/taskflow/test_finish_gate.py`
  - 所有 step done + evidence passed → finish 成功
  - 有 step not done → finish 被拒绝
  - 有 step blocked → finish 被拒绝
  - SisyphusVerifier 门控未通过 → finish 被拒绝
  - evidence 有 failed 记录 → finish 被拒绝
  - evidence 有 stale 记录 → finish 被拒绝（或警告）

- [ ] **T4.2** — `tests/agent/middlewares/test_completion_drain_gate.py`
  - enforce_verification=False → 文本提醒（当前行为）
  - enforce_verification=True, evidence all passed → 无干预
  - enforce_verification=True, evidence missing → 注入 gate 提醒

---

## 8. 实施顺序

```
Phase 1（StepJudge — 步骤级 LLM 判别器）  ← 依赖 Phase 3 的 evidence_collector
  │
  ├─ Step 1.1  step_judge.py 模块
  ├─ Step 1.2  taskflow_resume.py 修改
  ├─ Step 1.3  taskflow_dispatch.py 修改（judge feedback 注入）
  ├─ 配置      config/features/agent_side/step_judge.py
  └─ 测试      T1.1-T1.2

Phase 2（CompletionJudge — 子代理 goal loop）  ← 依赖 Phase 1 + Phase 3
  │
  ├─ Step 2.1  completion_judge.py 模块
  ├─ Step 2.2  spawn/core.py goal loop 修改
  ├─ Step 2.3  spawn_subagent_direct() + sessions_spawn schema
  ├─ 配置      config/features/agent_side/completion_judge.py
  └─ 测试      T2.1-T2.2

Phase 3（Evidence Ledger 接入生产）            ← 最先实施，Phase 1/2 依赖它
  │
  ├─ Step 3.0  EvidenceLedger 类扩展（for_session/list_records/mark_stale_for_path）
  ├─ Step 3.1  terminal/python_repl 自动记录
  ├─ Step 3.2  file_tools/ write_file/patch_file stale 标记
  ├─ Step 3.3  evidence_collector.py
  ├─ 配置      config/features/agent_side/evidence_ledger.py
  └─ 测试      T3.0-T3.2

Phase 4（Finish Gate — SisyphusVerifier 接入） ← 依赖 Phase 1 + 3
  │
  ├─ Step 4.1  taskflow_finish.py 前置门控
  ├─ Step 4.2  SubagentCompletionDrainMiddleware 升级
  └─ 测试      T4.1-T4.2
```

---

## 9. 回滚方案

### Phase 1 回滚

1. `step_judge.py` 可删除——`taskflow_resume` 回退到直接 `mark_step_done()`
2. `STEP_JUDGE["enabled"] = False` 即可全局禁用，judge 调用跳过（fail-open PASS）
3. 不影响现有 step 的 status 转换逻辑

### Phase 2 回滚

1. `COMPLETION_JUDGE["enabled"] = False`（默认即为 False）→ goal loop 禁用，回退到单次执行
2. `spawn_subagent_direct()` 的 `goal_loop` 参数默认 False，不传时与当前行为完全一致
3. `completion_judge.py` 可删除

### Phase 3 回滚

1. `EVIDENCE_LEDGER["auto_record"] = False` → 不自动记录，EvidenceLedger 仍可手动使用
2. `auto_stale = False` → 不自动标记 stale
3. 现有 `EvidenceLedger` 和 `SisyphusVerifier` 不受影响（它们已经在代码库中）

### Phase 4 回滚

1. `taskflow_finish` 中的 SisyphusVerifier 调用可通过配置开关禁用
2. `SubagentCompletionDrainMiddleware` 的 `enforce_verification` 默认 False → 回退到文本提醒

---

## 10. 文件变更清单

### 新建文件

| 文件路径                                                | 用途                                   |
| ------------------------------------------------------- | -------------------------------------- |
| `agent/tools/taskflow/step_judge.py`                    | StepJudge — LLM 步骤结果判别器         |
| `agent/tools/subagent/spawn/completion_judge.py`        | CompletionJudge — LLM 子代理完成判别器 |
| `agent/tools/taskflow/evidence_collector.py`            | Evidence summary 收集器                |
| `config/features/agent_side/step_judge.py`              | StepJudge 配置                         |
| `config/features/agent_side/completion_judge.py`        | CompletionJudge 配置                   |
| `config/features/agent_side/evidence_ledger.py`         | EvidenceLedger 配置                    |
| `tests/agent/tools/taskflow/test_step_judge.py`         | StepJudge 测试                         |
| `tests/agent/tools/taskflow/test_resume_with_judge.py`  | Resume+Judge 集成测试                  |
| `tests/agent/tools/subagent/test_completion_judge.py`   | CompletionJudge 测试                   |
| `tests/agent/tools/subagent/test_goal_loop.py`          | Goal loop 集成测试                     |
| `tests/agent/tools/test_evidence_auto_record.py`        | Evidence 自动记录测试                  |
| `tests/agent/tools/test_evidence_stale.py`              | Evidence stale 标记测试                |
| `tests/agent/tools/taskflow/test_finish_gate.py`        | Finish gate 测试                       |
| `tests/agent/middlewares/test_completion_drain_gate.py` | Completion drain gate 测试             |

### 修改文件

| 文件路径                                              | 变更内容                                                                                                 |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `agent/tools/todolist/evidence_ledger.py`             | 扩展：新增 `for_session()` / `read_for_session()` / `mark_stale_for_path()` + `SessionEvidenceLedger` 类 |
| `agent/tools/taskflow/tools/taskflow_resume.py`       | 插入 StepJudge 调用；RETRY/BLOCK 路径；移除 validation_criteria 警告文本                                 |
| `agent/tools/taskflow/tools/taskflow_dispatch.py`     | re-dispatch 时注入 judge_feedback                                                                        |
| `agent/tools/taskflow/tools/taskflow_finish.py`       | 插入 Finish gate（DAG 完整性 + evidence + SisyphusVerifier 适配）                                        |
| `agent/tools/subagent/spawn/core.py`                  | `_execute_subagent()` 增加 goal loop 模式；`spawn_subagent_direct()` 增加 goal_loop/goal_max_turns 参数  |
| `agent/tools/__init__.py`                             | sessions_spawn schema 暴露 goal_loop 参数                                                                |
| `agent/tools/terminal.py`                             | 自动记录验证命令 evidence                                                                                |
| `agent/tools/python_repl.py`                          | 自动记录验证命令 evidence                                                                                |
| `agent/tools/file_tools/write_file.py`                | 文件编辑后标记 evidence stale                                                                            |
| `agent/tools/file_tools/patch_file.py`                | 文件编辑后标记 evidence stale                                                                            |
| `agent/middlewares/subagent_completion_drain/core.py` | 升级为可选程序门控（类属性 `enforce_verification`）                                                      |
| `agent/core.py`                                       | 实例化 SubagentCompletionDrainMiddleware 后设置 `enforce_verification`                                   |
| `config/features/__init__.py`                         | re-export 三个新配置                                                                                     |
| `config/features/agent_side/__init__.py`              | re-export 三个新配置                                                                                     |

---

## 11. 与 hermes-agent 的关键差异

| 维度                | hermes-agent                                      | Sherry (本计划)                                      | 理由                                                                                    |
| ------------------- | ------------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Judge 模型          | 独立 `auxiliary.goal_judge` 配置                  | `build_auxiliary_llm(task=...)`                      | Sherry 已有 auxiliary LLM 体系，复用                                                    |
| Judge 输入          | task title+body + last_response + bg_processes    | step.task + criteria + result_text + evidence        | Sherry 有 validation_criteria 字段，更结构化                                            |
| Verdict             | done/continue/wait (3 值)                         | PASS/RETRY/BLOCK (step) + DONE/CONTINUE (completion) | Sherry 的 RETRY 对应 hermes 的 CONTINUE；BLOCK 是 Sherry 独有（DAG 步骤可标记 blocked） |
| Goal Loop           | kanban worker 循续轮                              | subagent goal loop                                   | 架构不同：hermes 用 kanban task，Sherry 用 subagent run                                 |
| Evidence            | `classify_verification_command()` 自动记录        | 复用现有 `EvidenceLedger` + 自动记录                 | Sherry 已有 EvidenceLedger，接入即可                                                    |
| Fail-open           | continue (不阻塞)                                 | PASS (step) / DONE (completion)                      | 一致：judge 不可用时不阻塞                                                              |
| 预算                | `goal_max_turns` (默认 20)                        | `goal_max_turns` (默认 5)                            | Sherry 子代理更轻量，5 轮足够                                                           |
| 完成门控            | `kanban_complete` 调 judge                        | `taskflow_finish` 调 SisyphusVerifier                | 架构不同：hermes 门控 kanban，Sherry 门控 taskflow                                      |
| Completion contract | `GoalContract` (outcome/verification/constraints) | `validation_criteria` (step 字段)                    | Sherry 已有字段，无需新增结构                                                           |
