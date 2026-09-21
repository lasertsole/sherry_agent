# TaskFlow Step Judge & 子代理完成门控计划（下）

> 本文接续 [STEP_JUDGE_PLAN_1.md](./STEP_JUDGE_PLAN_1.md)，包含 Phase 3-4 实现计划、配置设计、测试计划、实施顺序、回滚方案、文件变更清单及与 hermes-agent 的关键差异。

## 4. 实现计划（续）

### Phase 3：Evidence Ledger 接入生产

#### Step 3.0：扩展 EvidenceLedger 类（添加缺失方法）

- [ ] **修改 `agent/tools/todolist/evidence_ledger.py`**，添加计划所需的方法

当前 `EvidenceLedger` 是全局单例（单 JSONL 文件，`agent/tools/todolist/evidence_ledger.py:18`），只有 `append(entry: dict)` 和 `read_all() -> list[dict]`。需要扩展为支持 session 隔离和 stale 标记。

**模块契约是 append-only**（`evidence_ledger.py:4-6` docstring：`Appends never rewrite existing lines`）。因此 stale **不能**回写历史行、更不能「全量重写」——只能**追加一条 stale 事件行**，由读取端（Step 3.3）据此派生 stale 状态：

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
        """追加一条 stale 事件行（append-only，绝不改写历史行）。

        写入形如::

            {"event": "stale", "file_path": <path>, "session_id": <sid>}

        返回本次事件覆盖的当前有效 evidence 行数（仅用于日志）；stale 的最终
        判定由读取端完成（Step 3.3）。evidence 行的命令字段是单数 ``command``
        （见 Step 3.1 的 ``_record_evidence``），不是 ``commands`` 列表。
        """
        records = cls.read_all()
        count = 0
        for r in records:
            if r.get("event") == "stale":
                continue
            if session_key and r.get("session_id") != session_key:
                continue
            if file_path in str(r.get("command") or ""):
                count += 1
        cls.append({"event": "stale", "file_path": file_path, "session_id": session_key or ""})
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
    session_key: str | None = None,
    flow_id: str | None = None,
) -> str | None:
    """Build a human-readable evidence summary for the judge prompt.

    Callers scope by exactly one key: taskflow_resume passes ``session_key``
    (the step's child_session_key); taskflow_finish passes ``flow_id``.
    Returns None if no evidence recorded.

    STALENESS IS DERIVED, not stored: the ledger is append-only, so a
    non-stale evidence row is stale iff a LATER ``{"event": "stale"}`` row
    (same session scope) names a file_path contained in the row's ``command``.
    """
    sid = session_key or flow_id or "default"
    try:
        records = EvidenceLedger.for_session(sid).list_records()
    except AttributeError:
        # Phase 3 尚未实施——for_session 不存在时回退到全局 read_all
        records = [r for r in EvidenceLedger.read_all() if r.get("session_id") == sid]

    evidence_rows = [(i, r) for i, r in enumerate(records) if r.get("event") != "stale"]
    stale_events = [(i, r) for i, r in enumerate(records) if r.get("event") == "stale"]

    def _is_stale(index: int, row: dict) -> bool:
        command = str(row.get("command") or "")
        return any(
            j > index and str(ev.get("file_path") or "") in command
            for j, ev in stale_events
        )

    if not evidence_rows:
        return None

    lines = []
    for i, r in evidence_rows:
        status = r.get("status", "unknown")
        icon = "pass" if status == "passed" else "FAIL" if status == "failed" else "?"
        stale_tag = " [stale]" if _is_stale(i, r) else ""
        kind = r.get("kind", "unknown")
        cmd = r.get("command", "")
        lines.append(f"  [{icon}] {kind}: `{cmd}`{stale_tag}")

    return "\n".join(lines)
```

> 签名只保留 `session_key` / `flow_id` 两个**真实被使用**的参数（PLAN_1 的 `taskflow_resume` 传 `session_key=`，本文件 Gate C 传 `flow_id=`）。原来的 `step_id` 参数没有消费方（evidence 行不携带 step_id），已删除，避免又一个悬空参数。

---

### Phase 4：Finish Gate — SisyphusVerifier 接入生产

#### Step 4.1：修改 `taskflow_finish.py` — 插入 SisyphusVerifier 前置门控

- [ ] **修改 `taskflow_finish` 工具**，在标记 DONE 之前调用 SisyphusVerifier

**实际签名**：`SisyphusVerifier.verify()` 是 `@staticmethod async`，签名为 `(session_id: str, todo: dict, plan_path: str, checkbox_label: str) -> tuple[bool, dict]`。它面向 **todolist 系统**，需要一个适配层把「哪个 todo 对应哪个 plan checkbox」喂给它。

**现状（勿假设不存在的字段）**：

- `taskflow_finish` 用 `flow = await store_sqlite.get_flow(flow_id, session_id)` 取流（:32），`state = dict(flow["state"])`（:42）。
- `flow["state"]` **只有** `steps` / `results` / `summary`（外加 `default_state()` 保证的 `description` / `creator_session_key`）——**不存在 `flow_state` 这个变量，也不存在 `linked_todo` / `plan_path` / `todo_label` 这些 state 键**。旧稿整段建立在虚构字段之上。
- taskflow 家族的错误一律**直接 `return "Error: ..."`**（`_shared.py:1-7` 的 error-text 契约）；**没有 `tool_error` 助手**（`agent/tools/pub_base/tool_utils.py:6` 返回 JSON，taskflow 不用它）。

```python
# agent/tools/taskflow/tools/taskflow_finish.py — 修改后的 finish（示意；现状 :32 / :42 / :46-58）

from agent.tools.todolist.verifier import SisyphusVerifier

@tool("taskflow_finish")
async def taskflow_finish(
    flow_id: str,
    summary: str = "",
    expected_revision: int | None = None,
    todo: dict | None = None,          # NEW：调用方显式传入关联 todo（方案 a）
    plan_path: str | None = None,      # NEW：调用方显式传入 plan 路径
    checkbox_label: str | None = None, # NEW：对应 plan 上的 checkbox 文本
    session_id: SessionId = "",
) -> str:
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id, session_id)   # :32 现有
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )
    state = dict(flow["state"])       # :42 —— flow["state"] 只有 steps / results / summary
    steps = list(state.get("steps") or [])

    # Gate A: 所有步骤必须 done 或 blocked（DAG 完整性检查）
    pending = [
        s for s in steps
        if s["status"] not in (StepStatus.DONE.value, StepStatus.BLOCKED.value)
    ]
    if pending:
        return (
            f"Error: Cannot finish: {len(pending)} step(s) not done/blocked: "
            f"{[s['step_id'] for s in pending]}"
        )

    # Gate B: 有 blocked 步骤 → 拒绝 finish（需人工介入）
    blocked = [s for s in steps if s["status"] == StepStatus.BLOCKED.value]
    if blocked:
        return (
            f"Error: Cannot finish: {len(blocked)} step(s) are blocked: "
            f"{[s.get('block_reason', 'unknown') for s in blocked]}"
        )

    # Gate C: Evidence gate —— 有 failed/stale evidence 则拒绝
    from agent.tools.taskflow.evidence_collector import collect_evidence_summary
    evidence = collect_evidence_summary(flow_id=flow_id)
    if evidence and "FAIL" in evidence:
        return f"Error: Finish gate rejected: failing verification evidence:\n{evidence}"
    if evidence and "[stale]" in evidence:
        return (
            f"Error: Finish gate rejected: stale evidence (code changed since last "
            f"verification):\n{evidence}\nRe-run verification commands before finishing."
        )

    # Gate D: SisyphusVerifier —— 仅当调用方显式给了 todo + plan_path 才执行
    if todo and plan_path:
        passed, verify_evidence = await SisyphusVerifier.verify(
            session_id=session_id,
            todo=todo,
            plan_path=plan_path,
            checkbox_label=checkbox_label or "",
        )
        if not passed:
            reason = verify_evidence.get("reason", "unknown")
            return f"Error: Finish gate rejected by SisyphusVerifier: {reason}"

    if summary:
        state["summary"] = summary     # :43-44 现有

    try:
        updated = await store_sqlite.update_flow(
            flow_id, revision, session_id=session_id, state=state,
            wait=None, status=TaskFlowStatus.DONE.value,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    return (
        f"TaskFlow finished: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"status={updated['status']}"
    )
```

> **Gate D 关联字段设计（选定方案 a）**：现状 `flow["state"]` 没有关联 todo/plan 的字段。
> - **方案 a（采用）**：给 `taskflow_finish` 增加可选 `todo` / `plan_path` / `checkbox_label` 参数，由**持有 todolist 与 plan 路径的主代理**在 finish 时显式传入；不传则跳过 Gate D。**零迁移、零 schema 变更**，Gate D 的取数路径全部真实存在。
> - **方案 b（不采用）**：给 flow/step 定义 `linked_todo` / `plan_path` 关联字段并写入 `state_json`——需要 DB 迁移（`store_sqlite.py` 已有 `session_id` 列 :69 与 `_SESSION_ID_COLUMN_DDL` :107 先例），且关联关系的现成落点已在 todo 侧（`todo["flow_id"]` / `todo["step_id"]`，`SisyphusVerifier` Gate 4 正是从 todo 读 flow），重复建模。

#### Step 4.2：修改 `SubagentCompletionDrainMiddleware` — 升级为程序门控

- [ ] **修改 `SubagentCompletionDrainMiddleware`**

**实际结构**：继承 `AgentMiddleware`，**无自定义 `__init__`**。实现 `abefore_model` / `before_model`（**不是 `after_model`**）。**仅在 `agent/core.py` 中无参实例化**（`_build_middlewares()` :180）；子代理中间件列表（`agent/tools/subagent/spawn/core.py:846-878`）**不含**它，故本次升级只影响主代理。

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

**接线方式**：`enforce_verification` 作为类属性，在 `agent/core.py::_build_middlewares()`（:139）里**先把 drain 提取为局部变量再设属性**，然后放进列表：

```python
# agent/core.py — _build_middlewares()（:139-205）中；drain 当前在 :180 无参实例化
drain_mw = SubagentCompletionDrainMiddleware()
drain_mw.enforce_verification = EVIDENCE_LEDGER.get("enforce_on_complete", False)  # 或新配置项
# ... 中间件列表里用 drain_mw 替换原来的 SubagentCompletionDrainMiddleware() ...
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
  └─ 无依赖。_MAIN_REQUIRED / _SUBAGENT_REQUIRED 目前仅存在于
     TODO/DEEPAGENTS_BORROWING_PLAN.md:200/218，**尚未实现**（待 P1-6 实施）；
     待其落地后，本计划若新增中间件，才需要同步更新这两个元组。
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
    """RETRY verdict 的最大重试次数（复用 step 的 retry_count 预算）。默认 2。"""
    max_result_chars: int
    """传给 judge 的 result_text 截断长度。默认 8000。"""
    evidence_aware: bool
    """是否在 judge prompt 中包含 evidence summary。默认 True。"""

STEP_JUDGE = StepJudgeConfig(
    enabled=True,
    max_retries=2,
    max_result_chars=8000,
    evidence_aware=True,
)
```

> **无 `llm_task` 键**：`build_auxiliary_llm(temperature=...)`（`models/LLMs/auxiliary_llm/core.py:48`）**没有 `task=` 参数**，该键没有消费方，已删除。

- [ ] **新建 `config/features/agent_side/completion_judge.py`**

```python
from typing import TypedDict

class CompletionJudgeConfig(TypedDict):
    enabled: bool
    """全局开关。False 时 goal loop 禁用，子代理单次执行。"""
    goal_max_turns: int
    """goal loop 最大轮次（与 PLAN_1 的 `goal_max_turns` 参数同名）。默认 5。"""

COMPLETION_JUDGE = CompletionJudgeConfig(
    enabled=False,  # 默认关闭，需要显式开启
    goal_max_turns=5,
)
```

> **命名统一**：旧稿的 `max_turns` 已改为 `goal_max_turns`，与 PLAN_1 Step 2.2/2.3 的 `_execute_subagent(goal_max_turns=...)` / `spawn_subagent_direct(goal_max_turns=...)` 完全一致。**无 `llm_task` / `fail_open_verdict` 键**：`build_auxiliary_llm` 无 `task=` 参数；fail-open 固定为 `DONE`（设计上绝不把子代理困在 goal loop 里），没有可配置消费方。

- [ ] **新建 `config/features/agent_side/evidence_ledger.py`**

```python
from typing import TypedDict

class EvidenceLedgerConfig(TypedDict):
    auto_record: bool
    """是否自动记录 terminal/python_repl 的验证命令结果。默认 True。"""
    auto_stale: bool
    """是否在文件编辑后自动追加 stale 事件。默认 True。"""

EVIDENCE_LEDGER = EvidenceLedgerConfig(
    auto_record=True,
    auto_stale=True,
)
```

> **无 `ledger_path` 键**：路径由仓库统一约定 `/` 解析——`config/path.py::resolve_evidence_ledger_path()`（:212）返回 `SRC_DIR/data/evidence-ledger.jsonl`，`EvidenceLedger.LEDGER_PATH`（`evidence_ledger.py:23`）直接采用它。硬编码 `"data/evidence-ledger.jsonl"` 既偏离仓库约定（一律 `SRC_DIR/data/`），又是 cwd 相对路径。测试需要重定向时改 `EvidenceLedger.LEDGER_PATH`（类属性，见其 docstring :8），不需要配置键。

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
  - PASS verdict → step marked done（内联 `status=DONE`）, dependents unlocked
  - RETRY verdict（`step_retry_count(step) < STEP_JUDGE["max_retries"]`）→ 经 `spawn_replacement` + `apply_redispatch` 重派，`child_session_key` 更新、`retry_count+1`、`judge_feedback` 写入 step
  - RETRY verdict（预算耗尽）→ step BLOCKED, block_reason set（fail-open）
  - BLOCK verdict → step BLOCKED
  - judge 不可用 → fail-open PASS, step marked done
  - `taskflow_dispatch` 重派时 judge_feedback 拼进 task，且派发后被清除

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

1. `step_judge.py` 可删除——`taskflow_resume` 回退到**内联** `step["status"] = str(StepStatus.DONE)`（`agent/tools/taskflow/tools/taskflow_resume.py:148` 的原逻辑），或改用 `_shared.mark_step_done(steps, child_session_key)`（`agent/tools/taskflow/tools/_shared.py:138`）
2. `STEP_JUDGE["enabled"] = False` 即可全局禁用，judge 调用跳过（fail-open PASS）
3. 不影响现有 step 的 status 转换逻辑

### Phase 2 回滚

1. `COMPLETION_JUDGE["enabled"] = False`（默认即为 False）→ goal loop 禁用，回退到单次执行
2. `spawn_subagent_direct()` 的 `goal_loop` 参数默认 False，不传时与当前行为完全一致
3. `completion_judge.py` 可删除

### Phase 3 回滚

1. `EVIDENCE_LEDGER["auto_record"] = False` → 不自动记录，EvidenceLedger 仍可手动使用
2. `auto_stale = False` → 不自动追加 stale 事件行
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
| `agent/tools/todolist/evidence_ledger.py`             | 扩展：新增 `for_session()` / `read_for_session()` / `mark_stale_for_path()`（**追加 stale 事件行**，append-only）+ `SessionEvidenceLedger` 类 |
| `agent/tools/taskflow/tools/taskflow_resume.py`       | 在 `:146-157` 块插入 StepJudge；RETRY 复用 `_retry` 重派；BLOCK；移除 validation_criteria 警告文本        |
| `agent/tools/taskflow/tools/taskflow_dispatch.py`     | `:114-121` 派发前把 `step["judge_feedback"]` 拼进 task，派发后清除                                        |
| `agent/tools/taskflow/tools/_shared.py`               | `apply_dispatched_steps` 落库时对匹配 step 一并清除 `judge_feedback`（避免残留）                          |
| `agent/tools/taskflow/tools/taskflow_finish.py`       | 插入 Finish gate（DAG 完整性 + evidence + SisyphusVerifier）；新增可选 `todo`/`plan_path`/`checkbox_label` |
| `agent/tools/subagent/spawn/core.py`                  | `_execute_subagent()` 增加 goal loop 模式；`spawn_subagent_direct()` 增加 goal_loop/goal_max_turns 参数  |
| `agent/tools/subagent/tools/sessions_spawn.py`        | `SessionsSpawnSchema`(:21) + `_arun`(:70) 暴露 goal_loop/goal_max_turns，并透传 `spawn_subagent_direct()`(:102-115) |
| `agent/tools/terminal.py`                             | 自动记录验证命令 evidence                                                                                |
| `agent/tools/python_repl.py`                          | 自动记录验证命令 evidence                                                                                |
| `agent/tools/file_tools/write_file.py`                | 文件编辑后追加 evidence stale 事件                                                                       |
| `agent/tools/file_tools/patch_file.py`                | 文件编辑后追加 evidence stale 事件                                                                       |
| `agent/middlewares/subagent_completion_drain/core.py` | 升级为可选程序门控（类属性 `enforce_verification`）                                                      |
| `agent/core.py`                                       | 在 `_build_middlewares()`(:139) 先把 drain 提为局部变量，再设 `enforce_verification`（:180）             |
| `config/features/__init__.py`                         | re-export 三个新配置                                                                                     |
| `config/features/agent_side/__init__.py`              | re-export 三个新配置                                                                                     |
| `tests/config/test_features_agent_side.py`            | 把三个新配置加入 `INSTANCE_TYPED_DICT_PAIRS`(:62) 与 `SPOT_DEFAULTS`(:99) —— 该约定测试的硬契约        |

---

## 11. 与 hermes-agent 的关键差异

| 维度                | hermes-agent                                      | Sherry (本计划)                                      | 理由                                                                                    |
| ------------------- | ------------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Judge 模型          | 独立 `auxiliary.goal_judge` 配置                  | `build_auxiliary_llm(temperature=0)`                 | factory 无 `task=` 参数（`models/LLMs/auxiliary_llm/core.py:48`）；复用现有 auxiliary LLM 体系 |
| Judge 输入          | task title+body + last_response + bg_processes    | step.task + criteria + result_text + evidence        | Sherry 有 validation_criteria 字段，更结构化                                            |
| Verdict             | done/continue/wait (3 值)                         | PASS/RETRY/BLOCK (step) + DONE/CONTINUE (completion) | Sherry 的 RETRY 对应 hermes 的 CONTINUE；BLOCK 是 Sherry 独有（DAG 步骤可标记 blocked） |
| Goal Loop           | kanban worker 循续轮                              | subagent goal loop                                   | 架构不同：hermes 用 kanban task，Sherry 用 subagent run                                 |
| Evidence            | `classify_verification_command()` 自动记录        | 复用现有 `EvidenceLedger` + 自动记录                 | Sherry 已有 EvidenceLedger，接入即可                                                    |
| Fail-open           | continue (不阻塞)                                 | PASS (step) / DONE (completion)                      | 一致：judge 不可用时不阻塞                                                              |
| 预算                | `goal_max_turns` (默认 20)                        | `goal_max_turns` (默认 5)                            | Sherry 子代理更轻量，5 轮足够                                                           |
| 完成门控            | `kanban_complete` 调 judge                        | `taskflow_finish` 调 SisyphusVerifier                | 架构不同：hermes 门控 kanban，Sherry 门控 taskflow                                      |
| Completion contract | `GoalContract` (outcome/verification/constraints) | `validation_criteria` (step 字段)                    | Sherry 已有字段，无需新增结构                                                           |

---

## 12. 已存在、勿重建（执行前必读）

以下组件/助手**已在代码库中实现**，本计划只做接线，禁止重造：

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| `mark_step_done(steps, child_session_key)` | `agent/tools/taskflow/tools/_shared.py:138` | 幂等标记 step 为 done（resume 当前走内联赋值） |
| `unlock_dependents(steps)` | `agent/tools/taskflow/tools/_shared.py:153` | 单趟解锁 blocked→ready |
| `_retry` 全套 | `agent/tools/taskflow/tools/_retry.py` | `classify_failure` :56、`normalize_policy` :67、`step_retry_count` :87、`apply_redispatch` :170、`exhausted_note` :178 |
| `build_auxiliary_llm(temperature=...)` | `models/LLMs/auxiliary_llm/core.py:48` | **无 `task=` 参数** |
| `EvidenceLedger` | `agent/tools/todolist/evidence_ledger.py:18` | append-only；路径由 `resolve_evidence_ledger_path()`（`config/path.py:212`）解析 |
| `SisyphusVerifier` | `agent/tools/todolist/verifier.py:218` | `verify()` 是 `@staticmethod async`，签名 `(session_id, todo, plan_path, checkbox_label)` |
| `_VERIFICATION_REMINDER` | `agent/middlewares/subagent_completion_drain/core.py:42` | 现有提示文本 |
| taskflow `session_id` 列 | `agent/tools/taskflow/registry/store_sqlite.py:69`；迁移先例 `_SESSION_ID_COLUMN_DDL` :107 | flow 已有 session 隔离列，勿再加 |
| `tool_error(...)` | `agent/tools/pub_base/tool_utils.py:6` | 返回 JSON；**taskflow 家族不用它**，一律 `return "Error: ..."` |
| `build_agent_config(session_id=...)` | `pub/func/build_agent_config.py:6` | 子代理 ainvoke config 构造器 |

### 交叉引用自检

- 本文 §Phase 3 定义的 `collect_evidence_summary(session_key=…, flow_id=…)` 是 PLAN_1 Step 1.2（传 `session_key=`）与本文 Gate C（传 `flow_id=`）的**唯一**定义，签名一致。
- PLAN_1 Step 2.2 的 `goal_max_turns` 与本文 §6 `COMPLETION_JUDGE["goal_max_turns"]`、PLAN_1 Step 2.3 的 `spawn_subagent_direct(goal_max_turns=…)` 三处同名。
- PLAN_1 与本文的 StepJudge/CompletionJudge 均调用 `build_auxiliary_llm(temperature=0)`，无 `task=` 参数。
- 本文回滚章节指向的 `taskflow_resume.py:148` / `_shared.py:138` / `_build_middlewares()` :139 / drain :180 均在真实源码中。
