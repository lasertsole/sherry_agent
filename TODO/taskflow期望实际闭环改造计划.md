# TaskFlow 期望-实际闭环改造计划

> **项目**: sherry_agent-main
> **创建日期**: 2026-09-28
> **参考**: hermes-agent GoalContract/GoalState、deepagents quickjs `task()` responseSchema
> **前置依赖**: 无（本改造在现有 taskflow 工具族内进行，不依赖 PTC 或 subagent-role-migration）
> **状态**: 待执行

---

## 一、背景与问题

### 1.1 现状

Sherry 的 TaskFlow 是一个持久化的多步任务流系统，通过 SQLite 存储 flow 状态，支持 DAG 依赖、乐观锁、子 Agent 分派、结果注入、等待和重试。但当前 step 数据结构**缺少期望-实际闭环**：

```python
# 当前 step dict
{
    "step_id": "step-1",
    "task": "审查 src/auth.ts 的 SQL 注入",     # ← 期望参数：仅自由文本
    "depends_on": [],
    "status": "done",                             # ← DONE 只表示"结果已注入"，不表示成功
    "retry_count": 0,
    "child_session_key": "...",
    "validation_criteria": "检查所有 SQL 查询"    # ← 期望结构：仅自由文本，可选
}

# 当前 result record（在单独的 results list 中）
{
    "child_session_key": "...",
    "result": "发现 3 个注入点...",               # ← 实际结果：仅自由文本
    "result_hash": "...",
    "injected_at": 1234567890
}
```

### 1.2 SKILL.md 自己承认的限制

SKILL.md "Known limitations" 节原文：

> - A step being `done` only means "the result has been injected"; it does **not** mean the child session succeeded. At this stage there is no step-level `failed` / `skipped` status, and unlock/retry is not failure-aware: even if the child session fails, its `taskflow_resume` still marks the step `done` and unlocks successor steps.

### 1.3 对比 hermes 和 deepagents

| 维度 | hermes | deepagents | sherry (当前) |
|------|--------|------------|-------------|
| 期望参数 | GoalContract.outcome/constraints | `task()` description + subagentType | `task` 自由文本 |
| 期望结构 | GoalContract.verification | `responseSchema` (JSON Schema) | `validation_criteria` 自由文本 |
| 实际结果 | Run.summary/metadata | task() 返回结构化 JS 值 | `result` 自由文本 |
| 期望-实际配对 | GoalState 同时持有 contract + last_verdict | responseSchema 校验返回值 | 期望在 step，实际在 results list，分离 |
| 判定机制 | judge 模型对照 contract 产 verdict | schema 校验自动通过/失败 | "工具本身不评估成功/失败"，全靠 orchestrator LLM → **改造后**：Tier 1 schema 结构校验 + Tier 2 LLM judge 语义判定 |
| 步骤间数据传递 | — | — | 无（模型手动复制文本） |
| 步骤级模型选择 | Task.assignee/profile/model_override | `task()` subagentType | 无（所有 step 用同一 spawn 路径） |

---

## 二、缺失机制全览

### A. 期望-实际闭环缺失（5 项）

| ID | 缺失机制 | 影响 | 参考来源 |
|----|----------|------|----------|
| A1 | **无结构化期望参数** (`expected_params`) | 无法声明"此 step 期望输入 {file: ..., check_type: ...}"，只能靠自由文本 task 描述 | deepagents task() description + subagentType |
| A2 | **无期望结果 schema** (`response_schema`) | 无法声明"结果应为 {type: object, properties: {files: array, risk_count: number}}"，无法程序化校验 | deepagents responseSchema |
| A3 | **无结构化实际结果** (`structured_result`) | 结果仅文本，无法被下游 step 程序化消费 | deepagents task() 返回结构化值 |
| A4 | **期望与实际不在同一结构上** | 期望在 step.validation_criteria，实际在 results list 的 result 字段，分离难以配对比较 | hermes GoalState 同时持有 contract + last_verdict |
| A5 | **无 schema 校验标志** (`schema_validated`) | 无法区分"通过了 schema 校验"和"没有 schema / 校验失败" | deepagents |

### B. 判定机制缺失（4 项）

| ID | 缺失机制 | 影响 | 参考来源 |
|----|----------|------|----------|
| B1 | **无自动化结果校验** | `taskflow_resume` 明确说"工具本身不评估成功/失败"，每个 step 完成后都需一次 LLM 判定调用 | hermes judge 模型 |
| B2 | **无 step 级成功/失败状态** | DONE 只表示"结果已注入"。子 Agent 失败后仍标 DONE 并解锁后续 step | hermes Run.outcome (done/blocked/timeout/crash) |
| B3 | **无 judge/语义验证机制** | 没有 LLM judge 对照期望做语义裁定（结果是否完整/可信/违反约束），没有 verdict/reason 字段。schema 只验结构不验语义 | hermes GoalState.last_verdict/last_reason |
| B4 | **失败分类仅基于文本匹配** | `classify_failure()` 匹配关键词（"error"/"failed"/"timeout"），无法检测结构化失败（合法 JSON 但缺 required 字段） | deepagents schema 校验 |
| B5 | **schema 与 judge 被误设为互斥** | 原计划"schema 通过即跳过 LLM"会漏掉语义错误（结构对但内容错/浅/越界）。两层应串联：schema 验结构→judge 验语义 | hermes judge + deepagents schema |

### C. 步骤间数据流缺失（2 项）

| ID | 缺失机制 | 影响 |
|----|----------|------|
| C1 | **无步骤间结构化数据传递** | Step B 无法说"用 step A 的 result.files 作为我的输入参数"。模型必须手动读 step A 文本结果并粘贴到 step B 的 task 描述中 |
| C2 | **无输出映射/数据绑定** | 无 `input_bindings: {"target_files": "step-A.result.files"}` 机制，无法自动管道结构化结果 |

### D. 步骤元信息缺失（4 项）

| ID | 缺失机制 | hermes 对应 | deepagents 对应 |
|----|----------|-------------|-----------------|
| D1 | **无 step 级模型/subagent 类型选择** | Task.assignee/profile/model_override | `task()` subagentType |
| D2 | **无 step 级优先级** | Task.priority | — |
| D3 | **无 per-step 超时** | Task.max_runtime_seconds | — |
| D4 | **无 per-step token/cost 追踪** | Run.metadata (token usage) | — |

### E. 生命周期缺陷（3 项）

| ID | 缺失机制 | 影响 |
|----|----------|------|
| E1 | **失败无感知的 DAG 解锁** | 子 Agent 失败后 resume 仍标 DONE 并解锁后续 step。失败 step 应阻止/跳过后续依赖 step |
| E2 | **无 step 级 skip/cancel** | 无法跳过或取消单个 step（只能取消整个 flow）。无 `SKIPPED` 状态 |
| E3 | **无 schema 失败的自动 retry** | 当前 retry 仅在文本分类失败时触发。schema 校验失败应也是 retry 信号 |

---

## 三、改造方案

### 3.1 改造后 Step 数据结构

```python
def new_step(
    step_id: str,
    task: str,
    *,
    depends_on: list[str] | None = None,
    status: StepStatus | str = StepStatus.READY,
    # ── 新增：期望参数 ──
    expected_params: dict | None = None,        # A1: 结构化输入参数
    response_schema: dict | None = None,         # A2: 期望结果 JSON Schema（Tier 1 结构校验）
    # ── 新增：语义判定 ──
    judge_criteria: str | None = None,           # B3: 语义判定标准（自然语言，Tier 2）
    judge_model: str | None = None,              # B3: 指定 judge 使用的模型
    # ── 新增：步骤元信息 ──
    subagent_type: str | None = None,            # D1: 指定子 Agent 类型
    step_timeout_seconds: float | None = None,   # D3: per-step 超时
    priority: int = 0,                            # D2: 优先级
    # ── 保留：原有字段 ──
    validation_criteria: str | None = None,      # 保留，与 response_schema 互补
    retry_policy: dict | None = None,
) -> dict:
    step = {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": str(status),
        "retry_count": 0,
    }
    if expected_params is not None:
        step["expected_params"] = expected_params
    if response_schema is not None:
        step["response_schema"] = response_schema
    if judge_criteria is not None:
        step["judge_criteria"] = judge_criteria
    if judge_model is not None:
        step["judge_model"] = judge_model
    if subagent_type is not None:
        step["subagent_type"] = subagent_type
    if step_timeout_seconds is not None:
        step["step_timeout_seconds"] = step_timeout_seconds
    if priority:
        step["priority"] = priority
    if validation_criteria:
        step["validation_criteria"] = validation_criteria
    if retry_policy is not None:
        step["retry_policy"] = retry_policy
    return step
```

#### 质量门分层配置说明

每个 step 可按需配置不同级别的质量门：

| 配置组合 | 质量门行为 | 适用场景 |
|----------|-----------|----------|
| 无 schema + 无 judge_criteria | 直接标 DONE（兼容旧行为） | 简单 step，不需要校验 |
| 只 `response_schema` | Tier 1 结构校验：通过→DONE，失败→retry/FAILED | 机械提取、格式转换 |
| 只 `judge_criteria` | Tier 2 语义判定：LLM judge 评估→done/continue/failed | 开放任务，无固定结构 |
| `response_schema` + `judge_criteria` | **两层串联**：schema 通过后仍跑 judge | 分析审查、需要语义判断的复杂 step |

### 3.2 改造后 Result Record 数据结构

```python
result_record = {
    # ── 原有字段 ──
    "child_session_key": child_session_key,
    "result": result,                    # 原始文本结果（保留，兼容）
    "result_hash": result_hash_value,
    "injected_at": time.time(),
    # ── 新增：结构化实际结果 ──
    "structured_result": parsed_result,  # A3: 按 response_schema 解析的结构化值
    "schema_validated": schema_ok,      # A5: 是否通过 schema 校验
    # ── 新增：step 级元信息 ──
    "step_outcome": "success"|"failure"|"partial"|"skipped",  # B2
    "step_id": step_id,                  # A4: 期望-实际配对关联键
    # ── 新增：判定信息 ──
    "judge_verdict": "done"|"continue"|"skipped" | None,  # B3
    "judge_reason": str | None,          # B3
    # ── 新增：步骤级 token/cost ──
    "token_usage": {"input_tokens": N, "output_tokens": M, "model_name": "..."},  # D4
    "estimated_cost": float,             # D4
}
```

### 3.3 改造后 StepStatus 枚举

```python
class StepStatus(StrEnum):
    """Per-step DAG status."""
    BLOCKED = "blocked"
    READY = "ready"
    DISPATCHED = "dispatched"
    DONE = "done"           # 成功完成
    FAILED = "failed"       # 新增：执行失败（B2）
    SKIPPED = "skipped"     # 新增：被跳过（E2）
    CANCELLED = "cancelled" # 新增：被取消（E2）
```

状态流转：
```
blocked → ready → dispatched → done     (成功)
                   ↓
                   failed              (失败)
                   ↓
                   skipped/cancelled   (跳过/取消)

failed step 的依赖者：
  - 默认: 保持 blocked（不自动解锁）       (E1 修复)
  - 可配置: retry_policy.on_failure = "skip_dependents" → 标记 skipped
```

### 3.4 新增 `input_bindings` 字段（步骤间数据传递）

```python
# step 上新增字段
{
    "step_id": "step-B",
    "task": "修复 step-A 发现的注入点",
    "depends_on": ["step-A"],
    # 新增：从上游 step 的结构化结果中提取字段作为本 step 的输入
    "input_bindings": {
        "target_files": "step-A.structured_result.files",    # C1/C2
        "risk_count": "step-A.structured_result.risk_count",
    },
}
```

`taskflow_dispatch` / `taskflow_run_task` 在分派子 Agent 时，自动解析 `input_bindings`，从上游 step 的 `structured_result` 中提取值，注入到分派给子 Agent 的 task 描述中。

---

## 四、工具修改清单

### 4.1 `taskflow_run_task` — 新增期望参数和 schema

```python
@tool("taskflow_run_task")
async def taskflow_run_task(
    flow_id: str,
    task: str,
    label: str | None = None,
    expected_revision: int | None = None,
    depends_on: list[str] | None = None,
    validation_criteria: str | None = None,
    retry_policy: dict | None = None,
    # ── 新增参数 ──
    response_schema: dict | None = None,         # A2: 期望结果 JSON Schema（Tier 1）
    expected_params: dict | None = None,          # A1: 结构化期望参数
    input_bindings: dict | None = None,           # C1/C2: 上游结果字段绑定
    subagent_type: str | None = None,             # D1: 子 Agent 类型
    step_timeout_seconds: float | None = None,    # D3: per-step 超时
    priority: int = 0,                            # D2: 优先级
    # ── 新增：语义判定 ──
    judge_criteria: str | None = None,            # B3: 语义判定标准（Tier 2）
    judge_model: str | None = None,              # B3: judge 模型
    session_id: SessionId = "",
) -> str:
```

修改点：
- `new_step()` 调用传入新字段
- `build_state()` 中的 dispatched step 也携带新字段
- `_dispatch.dispatch_child()` 增加 `subagent_type` 和 `input_bindings` 解析

### 4.2 `taskflow_resume` — 两层质量门 + step_outcome

```python
@tool("taskflow_resume")
async def taskflow_resume(
    flow_id: str,
    child_session_key: str = "",
    result: str = "",
    expected_revision: int | None = None,
    token_usage: dict | None = None,
    validation_criteria: str | None = None,
    # ── 新增参数 ──
    structured_result: dict | None = None,       # A3: 结构化实际结果
    step_outcome: str | None = None,              # B2: 调用方声明的结果状态
    session_id: SessionId = "",
) -> str:
```

新增逻辑（两层质量门，B1/B3/B4/B5）：
```python
step = find_step_by_child_key(steps, child_session_key)
schema = step.get("response_schema") if step else None
judge_criteria = step.get("judge_criteria") if step else None

# ──────────────────────────────────────────────
# Tier 1: 结构校验（程序化，免费）
# ──────────────────────────────────────────────
schema_ok = None
parsed_result = structured_result
if schema is not None:
    if parsed_result is None:
        try:
            parsed_result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            parsed_result = None
    if parsed_result is not None:
        schema_ok = validate_json_schema(parsed_result, schema)
    else:
        schema_ok = False  # 有 schema 但无法解析

    # Tier 1 FAIL → 结构坏了，直接 retry，不调 LLM judge
    if schema_ok is False:
        if should_retry_failure(step, result, schema_validated=False):
            await auto_redispatch(step, ...)
            return "schema validation failed, re-dispatched (no judge call)"
        else:
            step["status"] = str(StepStatus.FAILED)
            # 不解锁依赖者
            return "schema validation failed, marked FAILED, dependents blocked"
    # Tier 1 PASS → 继续 Tier 2（不跳过语义判定）

# ──────────────────────────────────────────────
# Tier 2: 语义判定（LLM judge，花 token）
# 只在配置了 judge_criteria 时运行
# ──────────────────────────────────────────────
judge_verdict = None
judge_reason = None
if judge_criteria is not None:
    # judge 拿到的是 Tier 1 已验证的 structured_result（如果有 schema）
    # 或原始 result 文本（如果无 schema），只需专注语义判断：
    # - 结果是否完整？漏了什么？
    # - 值是否可信？有证据支撑？
    # - 是否违反了 constraints/boundaries？
    judge_verdict, judge_reason = await run_judge(
        structured_result=parsed_result if schema is not None else None,
        result_text=result,
        judge_criteria=judge_criteria,
        task_description=step.get("task", ""),
        judge_model=step.get("judge_model"),
    )

    if judge_verdict == "done":
        step_outcome = "success"
    elif judge_verdict == "continue":
        # 语义不通过，但可以 retry
        if should_retry_failure(step, result, schema_validated=schema_ok):
            await auto_redispatch(step, ...)
            return f"judge verdict=continue ({judge_reason}), re-dispatched"
        else:
            step_outcome = "failure"
    elif judge_verdict == "failed":
        step_outcome = "failure"
    # judge fail-open: 异常时默认 continue（不卡住，同 hermes 设计）
else:
    # 无 judge_criteria：
    #   - 有 schema 且通过 → 标 DONE（机械 step，结构对就是对）
    #   - 无 schema → 标 DONE（兼容旧行为）
    if step_outcome is None:
        step_outcome = "success"

# ──────────────────────────────────────────────
# B2/E1: 失败感知的 DAG 解锁
# ──────────────────────────────────────────────
if step_outcome == "failure":
    step["status"] = str(StepStatus.FAILED)
    # 不解锁后续依赖 step（保持 blocked）
elif step_outcome == "success":
    step["status"] = str(StepStatus.DONE)
    newly_ready, newly_skipped = unlock_dependents_failure_aware(steps, results)
```

**关键设计原则：**
- **Tier 1 FAIL 省掉 LLM 调用**：结构坏了直接 retry，不浪费 judge 调用
- **Tier 1 PASS 不跳过 Tier 2**：结构对不代表内容对，配置了 judge_criteria 的 step 仍跑语义判定
- **Tier 2 是可选的**：只有配置了 `judge_criteria` 才运行。机械 step（只 schema）不跑 judge
- **Judge fail-open**：judge 异常时默认 continue（不卡住循环），同 hermes 设计

### 4.3 `taskflow_dispatch` — 解析 input_bindings

```python
# 在 dispatch_child 调用前，解析 input_bindings
async def dispatch_with_bindings(step, steps, results, requester_key, label):
    bindings = step.get("input_bindings")
    if bindings:
        resolved_params = {}
        for param_name, binding_path in bindings.items():
            # binding_path 格式: "step-A.structured_result.files"
            parts = binding_path.split(".", 2)
            if len(parts) == 3:
                src_step_id, _, field_path = parts
                src_step = next(s for s in steps if s.get("step_id") == src_step_id)
                src_result = find_result(results, src_step.get("child_session_key"))
                value = extract_nested(src_result.get("structured_result"), field_path)
                resolved_params[param_name] = value
        # 注入到 task 描述
        task_text = step["task"] + "\n\n## Input parameters\n" + json.dumps(resolved_params)
    else:
        task_text = step["task"]

    subagent_type = step.get("subagent_type")
    return await _dispatch.dispatch_child(
        task=task_text,
        requester_session_key=requester_key,
        label=label,
        subagent_type=subagent_type,   # 新增参数
    )
```

### 4.4 `taskflow_summary` — 展示新字段

在 summary 输出中增加：
- `response_schema`: 有/无（有则显示 properties 列表）
- `expected_params`: 有则显示
- `input_bindings`: 有则显示绑定映射
- `judge_criteria`: 有则显示（Tier 2 语义判定标准）
- `judge_model`: 指定的 judge 模型
- `step_outcome`: success/failure/partial/skipped
- `schema_validated`: true/false/none
- `judge_verdict`: done/continue/failed/none
- `judge_reason`: judge 给出的理由
- `structured_result`: 截断显示
- `subagent_type`: 指定的子 Agent 类型
- per-step token/cost

### 4.5 `taskflow_progress` — 失败感知

- 统计中增加 `failed=` 和 `skipped=` 计数
- 失败 step 列入"需要决策"区域
- 不再将 failed step 计入"已完成"百分比

### 4.6 `_shared.py` — 新增 helper

```python
def validate_json_schema(value: Any, schema: dict) -> bool:
    """Validate value against a JSON Schema. Returns True/False."""
    try:
        import jsonschema
        jsonschema.validate(instance=value, schema=schema)
        return True
    except (jsonschema.ValidationError, Exception):
        return False

async def run_judge(
    *,
    structured_result: dict | None,
    result_text: str,
    judge_criteria: str,
    task_description: str,
    judge_model: str | None = None,
) -> tuple[str | None, str | None]:
    """Run an LLM judge to evaluate semantic quality of a step result.

    Tier 2 quality gate. Called only when step has judge_criteria.
    Returns (verdict, reason) where verdict is "done"|"continue"|"failed"|None.

    Fail-open: on any exception returns ("continue", error_msg) so a broken
    judge never wedges the flow (same design as hermes goal judge).

    The judge receives:
    - The task description (what was asked)
    - The judge_criteria (what "done" means, natural language)
    - The result (structured_result if available, else result_text)
    And must decide: is this semantically complete/correct/trustworthy?

    Verdict mapping:
    - "done" → step succeeds, mark DONE, unlock dependents
    - "continue" → not done, retry if budget remains, else mark FAILED
    - "failed" → definitively failed, mark FAILED, block dependents
    - None (exception) → fail-open to "continue"
    """
    from models.LLMs.main_llm import build_auxiliary_llm

    model = build_auxiliary_llm(model_name=judge_model)
    prompt = _build_judge_prompt(
        task_description=task_description,
        judge_criteria=judge_criteria,
        structured_result=structured_result,
        result_text=result_text,
    )
    try:
        response = await model.ainvoke([{"role": "user", "content": prompt}])
        raw = response.content if hasattr(response, "content") else str(response)
        verdict, reason = _parse_judge_response(raw)
        return verdict, reason
    except Exception as exc:
        logger.warning("taskflow judge failed, fail-open to continue: {}", exc)
        return "continue", f"judge error: {exc}"

def _build_judge_prompt(*, task_description, judge_criteria,
                        structured_result, result_text) -> str:
    """Build the judge prompt. Asks for JSON verdict."""
    result_section = (
        json.dumps(structured_result, ensure_ascii=False, default=str)
        if structured_result is not None
        else result_text[:2000]
    )
    return (
        "You are a strict judge evaluating whether a task step's result is complete and correct.\n\n"
        f"Task: {task_description}\n\n"
        f"Acceptance criteria (what 'done' means):\n{judge_criteria}\n\n"
        f"Result:\n{result_section}\n\n"
        "Decide:\n"
        '- "done": the result fully satisfies the criteria (with specific evidence, not a claim)\n'
        '- "continue": not done, but a retry could help (incomplete, shallow, wrong values)\n'
        '- "failed": definitively failed, retry won\'t help (constraint violation, wrong approach)\n\n'
        'Respond as JSON: {"verdict": "done|continue|failed", "reason": "<one sentence>"}'
    )

def _parse_judge_response(raw: str) -> tuple[str | None, str | None]:
    """Parse judge JSON response. Fail-open on unparseable output."""
    if not raw:
        return "continue", "judge returned empty response"
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
    try:
        parsed = json.loads(text)
        verdict = str(parsed.get("verdict", "continue")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()
        if verdict not in ("done", "continue", "failed"):
            verdict = "continue"
        return verdict, reason
    except (json.JSONDecodeError, TypeError):
        return "continue", f"judge reply was not JSON: {raw[:200]!r}"

def extract_nested(data: dict, path: str) -> Any:
    """Extract a nested value by dot-path: 'files.0.name' → data['files'][0]['name']."""
    current = data
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current

def unlock_dependents_failure_aware(
    steps: list[dict], results: list[dict]
) -> tuple[list[str], list[str]]:
    """Unlock dependents only when the step succeeded.

    Returns (newly_ready_ids, newly_blocked_by_failure_ids).
    A failed step keeps its dependents blocked; a skipped step
    marks its dependents as skipped (cascade skip).
    """
    newly_ready = []
    newly_skipped = []
    for step in steps:
        if step_status(step) != StepStatus.BLOCKED:
            continue
        if not deps_satisfied(step, steps):
            continue
        # Check if any dependency failed
        deps = step.get("depends_on") or []
        dep_steps = [s for s in steps if s.get("step_id") in deps]
        any_failed = any(step_status(d) == StepStatus.FAILED for d in dep_steps)
        any_skipped = any(step_status(d) == StepStatus.SKIPPED for d in dep_steps)

        if any_failed or any_skipped:
            step["status"] = str(StepStatus.SKIPPED)
            newly_skipped.append(step.get("step_id"))
        else:
            step["status"] = str(StepStatus.READY)
            newly_ready.append(step.get("step_id"))
    return newly_ready, newly_skipped
```

### 4.7 `_retry.py` — schema 失败的 retry

```python
def should_retry_failure(step: dict, result: str, *,
                         schema_validated: bool | None = None) -> bool:
    """True when a failure consumes a retry under the policy.

    Enhanced: schema validation failure (B4) is also a retry signal,
    not just text-classified failures.
    """
    policy = normalize_policy(step)
    if policy is None or retries_remaining(step) <= 0:
        return False

    # 新增：schema 校验失败 → 明确 retry
    if schema_validated is False:
        return True  # 结构化失败总是 retry（如果 retry_on 为空或含 "schema_error"）

    # 原有：文本分类失败
    failure_type = classify_failure(result)
    if failure_type is None:
        return False
    if not policy["retry_on"]:
        return True
    return failure_type in policy["retry_on"]
```

### 4.8 `_dispatch.py` — 支持 subagent_type

```python
async def dispatch_child(
    task: str,
    requester_session_key: str,
    label: str | None = None,
    subagent_type: str | None = None,     # 新增
) -> str:
    from agent.tools.subagent import spawn_subagent_direct

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        label=label,
        expects_completion_message=True,
        subagent_type=subagent_type,       # 新增：传递给 spawn
    )
    ...
```

### 4.9 `config.py` — 新增 StepOutcome 枚举

```python
class StepOutcome(StrEnum):
    """Per-step execution outcome (independent of DAG status)."""
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    SKIPPED = "skipped"
```

### 4.10 `store_sqlite.py` — 无需 schema 迁移

所有新字段存在 `state_json` 内的 step/result dict 中，不改变 DB 表结构。新字段对旧数据是可选的——旧 step 无 `response_schema` 时跳过校验，`step_outcome` 缺失时默认 `"success"`（兼容现有行为）。

---

## 五、新增依赖

```toml
# pyproject.toml
"jsonschema>=4.0.0,<5.0.0",  # JSON Schema 校验
```

---

## 六、测试计划

| 文件 | 标记 | 覆盖点 |
|------|------|--------|
| `tests/agent/tools/taskflow/test_schema_validation.py` | `unit` | Tier 1 结构校验通过/失败、无 schema 时跳过、structured_result 解析、schema_validated 标志 |
| `tests/agent/tools/taskflow/test_judge.py` | `unit` | Tier 2 语义判定：judge verdict=done→DONE、continue→retry/FAILED、failed→FAILED；judge 异常 fail-open=continue；prompt 构建正确性；JSON 解析 |
| `tests/agent/tools/taskflow/test_two_tier_gate.py` | `unit` | 两层串联：schema 通过+judge done→DONE；schema 通过+judge continue→retry；schema 通过+judge failed→FAILED；只 schema 无 judge→DONE；只 judge 无 schema→judge 用文本 |
| `tests/agent/tools/taskflow/test_step_outcome.py` | `unit` | step_outcome=success→DONE+unlock、failure→FAILED+不 unlock、skipped→级联 skip 依赖 |
| `tests/agent/tools/taskflow/test_input_bindings.py` | `unit` | 绑定解析、上游缺字段降级、嵌套路径提取、注入到 task 描述 |
| `tests/agent/tools/taskflow/test_retry_schema_failure.py` | `unit` | schema 校验失败触发 retry（不调 judge）、judge continue 触发 retry、retry_on 过滤、预算耗尽 |
| `tests/agent/tools/taskflow/test_subagent_type.py` | `unit` | subagent_type 传递到 spawn、None 时默认行为 |
| `tests/agent/tools/taskflow/test_backward_compat.py` | `module` | 旧 flow（无新字段）resume 仍标 DONE、unlock 仍正常、summary 不报错 |

### 关键测试用例

```python
# test_schema_validation.py
async def test_response_schema_pass_no_judge():
    """Step with only response_schema (no judge_criteria);
    result matches schema → schema_validated=True, step_outcome=success, status=DONE."""
    step = new_step("s1", "task", response_schema={
        "type": "object", "properties": {"files": {"type": "array"}}, "required": ["files"]
    })
    # resume with structured_result={"files": ["a.ts", "b.ts"]}
    # → schema_validated=True, step status=DONE (Tier 2 not configured, skip)

async def test_response_schema_fail_auto_retry():
    """Result missing required field → schema_validated=False,
    auto retry triggered, NO judge call made."""
    # resume with structured_result={"risk_count": 3}  # missing "files"
    # → schema_validated=False → should_retry=True → redispatch
    # → verify judge was NEVER called (Tier 1 FAIL skips Tier 2)

# test_judge.py
async def test_judge_verdict_done():
    """Judge says done → step marked DONE, dependents unlocked."""
    # step has judge_criteria="all SQL queries checked"
    # judge returns {"verdict": "done", "reason": "all 5 queries verified"}
    # → step_outcome=success, status=DONE

async def test_judge_verdict_continue_triggers_retry():
    """Judge says continue → retry if budget remains."""
    # judge returns {"verdict": "continue", "reason": "only checked 2 of 5 files"}
    # → should_retry=True → redispatch

async def test_judge_fail_open_on_exception():
    """Judge model raises → fail-open to continue, not crash."""
    # mock judge model to raise ConnectionError
    # → verdict="continue", reason contains "judge error"
    # → step NOT marked DONE, retry triggered if budget

# test_two_tier_gate.py
async def test_schema_pass_then_judge_still_runs():
    """Schema passes but judge still evaluates semantics (B5 fix)."""
    # step has response_schema + judge_criteria
    # structured_result matches schema (schema_validated=True)
    # BUT judge returns "continue" (result is shallow)
    # → step NOT marked DONE → retry triggered

async def test_schema_pass_judge_done():
    """Schema passes AND judge says done → DONE."""
    # schema_validated=True, judge verdict="done"
    # → step_outcome=success, status=DONE, dependents unlocked

async def test_only_judge_no_schema():
    """Step with judge_criteria but no response_schema → judge gets raw text."""
    # step has judge_criteria="comprehensive analysis" but no response_schema
    # → Tier 1 skipped (schema=None), Tier 2 runs with result_text
    # → judge evaluates text → done/continue/failed

# test_step_outcome.py
async def test_failure_blocks_dependents():
    """Step A fails → Step B (depends_on A) stays blocked, not unlocked."""
    # A: resume with step_outcome="failure" → A.status=FAILED
    # B: deps_satisfied → False (A is FAILED, not DONE) → B stays BLOCKED

async def test_skip_cascades():
    """Step A skipped → Step B (depends_on A) auto-skipped."""
    # A: status=SKIPPED
    # B: deps_satisfied → True, but any_dep_skipped → B auto-skipped

# test_backward_compat.py
async def test_old_flow_no_schema():
    """A flow created before this change (no response_schema) resumes normally."""
    # step has no response_schema → schema_validated=None, step_outcome="success"
    # → step status=DONE, dependents unlocked (legacy behavior)
```

---

## 七、实施步骤

| 步骤 | 内容 | 依赖 | 预估工时 |
|------|------|------|----------|
| 1 | `pyproject.toml` 添加 `jsonschema` 依赖 | 无 | 0.25h |
| 2 | `config.py` 新增 `StepOutcome` 枚举 + `StepStatus.FAILED/SKIPPED/CANCELLED` | 无 | 0.25h |
| 3 | `_shared.py` 新增 `validate_json_schema`、`extract_nested`、`unlock_dependents_failure_aware` | 步骤 2 | 1.5h |
| 4 | `_shared.py:new_step()` 增加新字段参数 | 步骤 3 | 0.5h |
| 5 | `_retry.py:should_retry_failure()` 增加 schema_validated 参数 | 步骤 3 | 0.5h |
| 6 | `_dispatch.py:dispatch_child()` 增加 subagent_type 参数 | 无 | 0.25h |
| 7 | `taskflow_run_task.py` 增加新参数 + input_bindings 解析 | 步骤 4,6 | 1.5h |
| 8 | `taskflow_resume.py` 两层质量门：Tier 1 schema + Tier 2 judge + step_outcome + 失败感知解锁 | 步骤 3,5 | 4h |
| 9 | `taskflow_dispatch.py` 调用 dispatch_with_bindings | 步骤 7 | 0.5h |
| 10 | `taskflow_summary.py` 展示新字段（含 judge_verdict/reason） | 步骤 8 | 0.5h |
| 11 | `taskflow_progress.py` 失败感知统计 | 步骤 8 | 0.5h |
| 12 | `taskflow_finish/fail/cancel.py` 适配 StepStatus.FAILED/SKIPPED | 步骤 2 | 0.5h |
| 13 | SKILL.md 更新文档（含两层质量门说明） | 步骤 7-11 | 1h |
| 14 | 编写测试（8 个文件） | 步骤 7-12 | 5h |
| 15 | `uv run --with ruff ruff check . && ruff format --check .` | 步骤 14 | 0.5h |
| 16 | `uv run --no-sync basedpyright agent/tools/taskflow/` | 步骤 15 | 0.5h |
| 17 | `uv run pytest tests/agent/tools/taskflow -q` | 步骤 16 | 1h |

**总预估：约 18.5 小时**

---

## 八、向后兼容性

| 场景 | 兼容策略 |
|------|----------|
| 旧 flow（无 response_schema、无 judge_criteria） | resume 时 schema=None, judge=None → `step_outcome="success"` → 标 DONE + unlock（行为不变） |
| 旧 flow（无 expected_params） | step 无 `expected_params` 字段 → dispatch 时 task 描述不含输入参数（行为不变） |
| 旧 flow（无 input_bindings） | step 无 `input_bindings` → dispatch 时 task 描述不追加参数（行为不变） |
| 旧 flow（无 structured_result） | resume 时 `structured_result=None` → 尝试从 result 文本解析 JSON → 失败则 `schema_validated=None`（行为不变） |
| 旧 flow（无 judge_criteria） | 不触发 Tier 2 judge → schema 通过即标 DONE（或无 schema 直接标 DONE） |
| 旧 step（无 subagent_type） | `subagent_type=None` → spawn 用默认路径（行为不变） |
| DB 表结构 | 无需迁移——所有新字段在 `state_json` 内 |
| 旧 StepStatus | DONE 仍表示成功完成；新增 FAILED/SKIPPED/CANCELLED 是增量 |

---

## 九、收益总结

| 改进点 | 收益 |
|--------|------|
| **两层质量门** (A2/A5/B1/B3/B4/B5) | Tier 1 结构校验失败→直接 retry 不调 judge（省 LLM 调用）；Tier 2 语义判定覆盖 schema 无法检测的内容错误（保准确率）。只配 schema 的机械 step 省 judge 调用；配 schema+judge 的复杂 step 两层都跑 |
| **step_outcome** (B2) | 失败 step 标 FAILED 而非 DONE，下游不被错误解锁 |
| **失败感知 DAG** (E1) | 失败 step 的依赖者保持 blocked 或级联 skip，不盲目执行后续 step |
| **结构化结果** (A3/C1) | 下游 step 程序化消费上游结果，无需 LLM 解析文本 |
| **input_bindings** (C2) | 自动管道结构化数据，减少模型手动复制文本的出错和 token 消耗 |
| **schema 失败 retry** (E3/B4) | 结构化失败也是 retry 信号，比文本关键词匹配更可靠 |
| **judge fail-open** (B3) | judge 模型异常时默认 continue 不卡住循环，同 hermes 设计 |
| **subagent_type** (D1) | 不同 step 可指定不同子 Agent 类型（reviewer/executor/researcher） |
| **per-step 超时** (D3) | 单步超时不拖垮整个 flow |
| **per-step token/cost** (D4) | 定位昂贵 step，优化成本 |

---

## 十、关键参考文件

### Sherry (本项目)

| 文件 | 用途 |
|------|------|
| `agent/tools/taskflow/tools/_shared.py:new_step` | step dict 构建——改造入口 |
| `agent/tools/taskflow/tools/taskflow_run_task.py` | step 注册 + 分派——新增参数 |
| `agent/tools/taskflow/tools/taskflow_resume.py` | 结果注入——新增 schema 校验 + step_outcome |
| `agent/tools/taskflow/tools/_retry.py:should_retry_failure` | 失败分类——新增 schema 信号 |
| `agent/tools/taskflow/tools/_dispatch.py:dispatch_child` | 分派 seam——新增 subagent_type |
| `agent/tools/taskflow/tools/taskflow_summary.py` | 只读回显——展示新字段 |
| `agent/tools/taskflow/config.py:StepStatus` | 状态枚举——新增 FAILED/SKIPPED/CANCELLED |
| `agent/tools/taskflow/registry/store_sqlite.py` | DB 存储——无需 schema 迁移 |
| `skills/builtin/core/taskflow/SKILL.md` | 用户文档——更新 |

### 外部参考

| 项目 | 文件 | 参考内容 |
|------|------|----------|
| `hermes-agent` | `hermes_cli/goals.py:294` GoalContract | 期望契约：outcome/verification/constraints/boundaries/stop_when |
| `hermes-agent` | `hermes_cli/goals.py:389` GoalState | 期望-实际配对：contract + last_verdict + last_reason |
| `hermes-agent` | `hermes_cli/kanban_db.py:1006` Run | 实际执行结果：outcome/summary/error/metadata |
| `deepagents` | `libs/partners/quickjs/README.md` task() | responseSchema 期望结构 + 结构化返回值 |
