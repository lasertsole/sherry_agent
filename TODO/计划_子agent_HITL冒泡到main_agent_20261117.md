# 计划：子 agent HITL 冒泡——访问模式继承 + 冒泡审批 + 计时器冻结

- 日期：2026-11-17（r2，加访问模式继承 + 共享放行表 + 计时器冻结）
- 状态：**待评审**
- 定位：(1) 子 agent 继承父 agent 的访问模式；(2) auto_edit 模式下子的 HITL 冒泡到父，父子共用放行参数表；(3) confirm_all 模式下子任何 HITL 默认不放行；(4) HITL 等待期间冻结所有计时器。
- 风险等级：**中-高** — 改访问模式继承 + ToolGuardrails + announce pipeline + 计时器冻结
- 依赖：高危操作防护计划（关键字扫描 + high_risk 标签）+ 现有 TaskFlow resume + announce pipeline
- 独立性：可独立实施——不依赖专家团/会议模式

---

## 0. 访问模式继承（新增）

### 0.1 现状

访问模式（`access_mode_service.py`）有 4 种：
- `plan` — 只读设计阶段
- `confirm_all` — 每个操作都需确认
- `auto_edit`（默认）— 正常审批门（危险/不确定才弹卡）
- `full_access` — YOLO，跳过所有 HITL

**当前：访问模式不传播给子 agent**（`child_agent_factory.py` 不引用 access_mode/yolo/confirm_all）。子 agent 的 HITL 行为由 `no_operator_in_scope()` 决定（子 agent 通常是 headless → 自动拒绝）。

### 0.2 目标：子 agent 继承父的访问模式

| 父访问模式 | 子继承 | 子的 HITL 行为 | 冒泡？ |
|-----------|--------|--------------|--------|
| `full_access` | `full_access`（SESSION_YOLO_KEY） | 跳过所有 HITL（包括关键字扫描） | ❌ 不冒泡 |
| `auto_edit` | `auto_edit` | 关键字命中 → [NEEDS_APPROVAL] → 冒泡到父 | ✅ 冒泡 |
| `confirm_all` | `confirm_all`（SESSION_CONFIRM_ALL_KEY） | 任何 HITL → 自动拒绝（不放行） | ❌ 不冒泡（直接拒绝） |
| `plan` | `plan`（PLAN_STATUS） | 只读工具，无写操作 | N/A |

### 0.3 实现：child_agent_factory 传播访问模式

**文件**：`agent/tools/subagent/spawn/child_agent_factory.py`

在 `_bind_child_run_context`（构建子 agent 时）中，读父的访问模式 flag，写入子的 state_register：

```python
def _bind_child_run_context(...):
    ...
    # 继承父的访问模式
    parent_session = requester_session_key
    yolo = state_register_mem.get_state(parent_session, SESSION_YOLO_KEY)
    confirm_all = state_register_mem.get_state(parent_session, SESSION_CONFIRM_ALL_KEY)

    if yolo:
        state_register_mem.set_state(child_session_id, SESSION_YOLO_KEY, True)
    elif confirm_all:
        state_register_mem.set_state(child_session_id, SESSION_CONFIRM_ALL_KEY, True)
    # auto_edit: 两个 flag 都不设（默认行为 = auto_edit）
```

### 0.4 full_access 模式下子 agent 跳过关键字扫描

子 agent 继承 `SESSION_YOLO_KEY=True` → ToolGuardrails 的关键字扫描检查 YOLO：

```python
# ToolGuardrails.wrap_tool_call
if is_yolo_mode(session_id):  # SESSION_YOLO_KEY=True
    # full_access 模式 → 跳过关键字扫描（包括高危+非幂等）
    # 但账本（executed=1 → block）仍生效
    if keyword_hit and keyword_entry.executed:
        return block  # 已执行过的操作仍 block
    # executed=0 → 放行（full_access 信任 agent）
    return await handler(request)
```

**注意**：full_access 下，关键字命中 + executed=0 → 放行（不审批）；关键字命中 + executed=1 → 仍 block（防重复执行）。账本不受 full_access 影响。

---

## 0.5 共享放行参数表（auto_edit 模式，新增）

### 设计

auto_edit 模式下，父和子**共用同一放行参数表**——用户同意子的放行的工具参数，则父也同样放行，反之亦然。

现有 `ToolApprovalStore`（`hitl/approval_store.py`）按 session_id 存储 approval。改为：子 agent 的 approval 查询路由到**父 session_id**。

### 实现

```python
# child_agent_factory.py
# 子 agent 的 approval store 使用父的 session_id
state_register_mem.set_state(child_session_id, "APPROVAL_SCOPE_PARENT", parent_session_id)

# ToolGuardrails / HITL handler 查 approval 时
def _get_approval_session_id(session_id: str) -> str:
    """子 agent 的 approval 查父的 session_id。"""
    parent = state_register_mem.get_state(session_id, "APPROVAL_SCOPE_PARENT")
    return parent if parent else session_id
```

当用户在父 agent 审批了子的工具调用（通过冒泡）：
1. 父调 `question` → 用户 approve → 父写 approval 到自己的 session_id
2. 新子 agent spawn → 继承 `APPROVAL_SCOPE_PARENT=父session_id`
3. 子的 ToolGuardrails 查 approval → 路由到父 session_id → 命中 → 放行

反过来：如果父直接调用了某个工具并获 approve，子调同工具同参数时也命中（共享 approval）。

### 0.6 confirm_all 模式下子 agent 自动拒绝

子继承 `SESSION_CONFIRM_ALL_KEY=True` → ToolGuardrails 的关键字扫描：

```python
# ToolGuardrails.wrap_tool_call
if is_confirm_all_mode(session_id):  # SESSION_CONFIRM_ALL_KEY=True
    # confirm_all 模式 → 任何需要 HITL 的操作 → 自动拒绝
    if keyword_hit:
        return ToolMessage(
            content="[BLOCKED: confirm_all mode] 此操作需要审批，但当前为变更前确认模式，默认不放行。",
            tool_call_id=request.tool_call_id,
            status="error"
        )
```

confirm_all 模式下不冒泡（直接拒绝）——因为用户已经选择了"变更前确认"= 不信任 agent 的任何变更，包括审批后的放行。

---

## 0.7 计时器冻结（HITL 等待期间，新增）

### 问题

HITL 等待用户确认期间，以下计时器会继续计时导致超时：

| 计时器 | 机制 | HITL 等待时 | 危险 |
|--------|------|-----------|------|
| **心跳停滞** | 后台定时器，每 1 min 检查。idle 7 min / in-tool 20 min → kill | **继续计时**（只有 question 工具设了 skip_heartbeat） | ⚠️ 用户审批慢 → agent 被杀 |
| 迭代预算 | 计数器（per model call） | 不计时（安全） | ✅ |
| Goal loop | 计数（per turn） | 不计时（安全） | ✅ |
| 子 agent 墙钟 | monotonic deadline | 子已完成（bubbling 设计中子在 HITL 前完成） | ✅ |
| sessions_yield 超时 | asyncio.wait_for 300s | yield 在 HITL 前已返回（子已完成） | ✅ |

**唯一需要冻结的是心跳停滞计时器。**

### 实现：HITL interrupt 时设 HEARTBEAT_SKIP

当前只有 `question` 工具的 `metadata["skip_heartbeat"]=True` 才跳过心跳。改为：**任何 HITL interrupt 触发时都设 HEARTBEAT_SKIP**。

**文件**：`agent/middlewares/humanInTheLoop/core.py`

在 `after_model` 调 `interrupt()` 前设 HEARTBEAT_SKIP：

```python
# HumanInTheLoop.after_model
async def aafter_model(self, state, response):
    ...
    for tool_call in tool_calls:
        ctx = self._build_approval_context(tool_call, state)
        consumed = await self._approval_registry.dispatch(tool_call, ctx)
        if consumed and ctx.outcome.interrupted:
            # HITL interrupt 即将发生 → 冻结心跳
            state_register_mem.set_state(session_id, HEARTBEAT_SKIP, True)
            raise GraphInterrupt(...)  # 原有逻辑
```

在 resume 路径（用户审批后恢复）清 HEARTBEAT_SKIP：

```python
# HumanInTheLoop 的 resume 处理（或 boot_recovery 的 resume）
# 用户审批后 graph 恢复时
state_register_mem.delete_state(session_id, HEARTBEAT_SKIP)
```

**同样适用于 HighRiskKeywordApprovalHandler**（在高危操作防护计划里）——它调 `interrupt()` 前也需要设 HEARTBEAT_SKIP。

### 0.8 子 agent 的计时器（bubbling 设计中不需要冻结）

在 bubbling 设计中：
- 子 agent 遇到关键字 → 返回 [NEEDS_APPROVAL] error → **子完成（TERMINAL）**
- 父 agent 调 question → HITL 等待
- **子在 HITL 等待开始前已完成** → 子没有运行中的计时器需要冻结

唯一需要冻结的是**父 agent 的心跳**（§0.7）。

如果未来添加 HITL 到子 agent 链（直接 interrupt 而非 bubbling），则需要冻结子的墙钟 + 心跳。当前 bubbling 设计不需要。

## 0. 架构事实

| 事实 | 来源 | 影响 |
|------|------|------|
| HITL 不在子 agent 链 | `specs.py:473` `required_in=frozenset({MAIN})` 无 child binding | 子 agent 无 HITL 审批能力 |
| `interrupt()` 在子 agent 里崩溃 | `GraphInterrupt` 继承 `BaseException`，lifecycle 的 `except Exception` 捕获不到 → spawn task 崩溃 → 子 graph 挂死无 resumer | 子 agent 不能调 `interrupt()` / `question` |
| 子 agent→父 agent 唯一通道 | 完成通知（TERMINAL）→ announce pipeline → 投递结果到父 agent | 子 agent 不能 mid-execution 问父 agent |
| TaskFlow auto-resume | `delivery.py::_try_auto_resume_taskflow` 子完成时自动 resume 父 flow step | 可在此注入 approval 上下文 |
| `question` 工具在子 agent 可用 | metadata 无 `scope: "main_only"` | 但调用会崩溃（同 interrupt）——实际不可用 |

---

## 1. 三段式冒泡设计

```
① 拦截：子 agent 调含高危关键字的工具
   → ToolGuardrails（child 链，required_in=_BOTH）扫描
   → 命中 → 返回 error: "[NEEDS_APPROVAL: keyword=X, tool=Y, args=...]"
   → 子 agent model 看到错误 → 无法继续 → 产出 result_text 含 [NEEDS_APPROVAL]

② 冒泡：子 agent 完成（TERMINAL）
   → announce pipeline 投递结果到父 agent
   → 父 agent model 读到 result_text 里的 [NEEDS_APPROVAL] 标记

③ 审批：父 agent 调 question("子 agent 需要执行含 X 的操作，是否允许？")
   → HITL interrupt（main 链有 HITL）
   → 用户 approve → 父 agent resume TaskFlow step + 注入 KEYWORD_APPROVALS
     → 新子 agent spawn → ToolGuardrails 检查 approval → 放行
   → 用户 reject → 父 agent resume step_outcome=blocked
```

---

## 2. 详细设计

### 2.1 ToolGuardrails 返回 NEEDS_APPROVAL（子 agent 侧）

**文件**：`agent/middlewares/tool_guardrails/core.py`

`wrap_tool_call`（在 main + child 链里）检查顺序：**账本（②）→ 关键字（③）**。

```python
async def wrap_tool_call(self, handler, request):
    tool = request.tool
    name = tool.name
    args_str = json.dumps(request.args, ensure_ascii=False)

    # ① 查 approval（审批后重新 spawn 的子 agent 携带）
    has_approval = self._check_approval(session_id, name, args_str)

    # ② 账本检查（high_risk=True AND idempotent=False 的工具）
    # 账本不受 YOLO / approval / allow policy 影响——系统级防护
    if self._should_ledger(tool):  # high_risk=True AND idempotent=False
        existing = ledger.check_executed(session_id, name, request.args)
        if existing:
            return ToolMessage(
                content=f"[NON-IDEMPOTENT GUARD] 此操作已执行过。首次结果: {existing['result_summary'][:200]}",
                tool_call_id=request.tool_call_id,
                status="error"
            )

    # ③ 关键字扫描（terminal/python_repl/MCP/execute_code）
    # write_file/patch_file/read_file 不扫描
    if self._should_scan_keywords(name) and not has_approval:
        # YOLO 跳过关键字扫描
        if is_yolo_mode(session_id):
            result = await handler(request)
            return ToolMessage(
                content=f"[YOLO: keyword scan skipped] {result.content}",
                tool_call_id=request.tool_call_id,
            )

        # 非 YOLO → 扫描（只扫 policy=block 的关键字）
        blockers = keyword_store.load_active_blockers(session_id)
        hit = scan_high_risk_keywords(args_str, blockers)
        if hit:
            # main agent: HITL handler 应该已经在 after_model 拦截了
            # 走到 wrap_tool_call 说明 HITL 没拦（YOLO / handler 顺序）→ 兜底
            # 子 agent: 无 HITL → 这里是唯一拦截 → 返回 NEEDS_APPROVAL
            return ToolMessage(
                content=(
                    f"[NEEDS_APPROVAL: keyword={hit.keyword}, tool={name}]\n"
                    f"This operation contains high-risk keyword '{hit.keyword}'.\n"
                    f"If you are the main agent: use the `question` tool to ask the user.\n"
                    f"If you are a subagent: include this [NEEDS_APPROVAL] block in your result."
                ),
                tool_call_id=request.tool_call_id,
                status="error"
            )

    # ④ 正常执行
    result = await handler(request)

    # 记录账本（high_risk=True AND idempotent=False）
    if self._should_ledger(tool) and result.status != "error":
        ledger.record(session_id, name, request.args, str(result.content)[:500])

    return result
```

**三个跳过条件**（精确）：

| 条件 | 跳过关键字？ | 跳过账本？ | 适用工具 |
|------|------------|-----------|---------|
| KEYWORD_APPROVALS 匹配 | ✅ 跳过 | ❌ 不跳过 | 所有扫描工具 |
| YOLO 模式 + 低危工具（terminal/python_repl） | ✅ 跳过 | ❌ 不跳过 | terminal/python_repl |
| YOLO 模式 + 高危+非幂等工具（MCP/execute_code） | ❌ **不跳过** | ❌ 不跳过 | MCP/execute_code |
| keyword policy=allow | ✅ 跳过（该关键字） | ❌ 不跳过 | 所有扫描工具 |

**YOLO 不可跳过高危+非幂等工具的关键字**：MCP 支付/execute_code 等不可逆操作，即使用户开了 YOLO 也必须逐次审批。terminal/python_repl 等可逆操作，YOLO 可跳过。

### 2.2 子 agent result_text 包含 NEEDS_APPROVAL

子 agent 的 model 看到 error `[NEEDS_APPROVAL: keyword=payment, tool=terminal]` 后：
- model 理解"我无法执行这个操作，需要人类审批"
- model 在 result_text 里包含这个标记：`"我需要执行部署操作，但 terminal(curl POST /api/payment) 被拦截。[NEEDS_APPROVAL: keyword=payment, tool=terminal, args=curl POST /api/payment]"`
- 子 agent 完成（TERMINAL，status=OK 或 BUDGET_EXHAUSTED）

这依赖 model 理解 `[NEEDS_APPROVAL]` 标记并在结果中保留它。通过 system prompt 引导：

```python
# 在子 agent 的 system prompt 或 ToolGuardrails 的 error message 里明确指示
"If you are a subagent: include this [NEEDS_APPROVAL] block verbatim in your result."
```

### 2.3 announce pipeline 投递结果到父 agent

**现有机制**（不改）：子 agent TERMINAL → `announce/core.py` → `delivery.py::_deliver_subagent_announcement` → 投递 `InboundMessage`（含 result_text）到父 agent 的消息流。

父 agent 的 model 在下一个 model call 看到子 agent 的结果消息，包含 `[NEEDS_APPROVAL]` 标记。

### 2.4 父 agent 识别 NEEDS_APPROVAL → 调 question

**文件**：`agent/middlewares/todo_continuation/core.py` 或 `agent/middlewares/reminder_catalog/sources.py`

在 continuation directive 或 reminder 里引导父 agent 识别 `[NEEDS_APPROVAL]`：

```python
# continuation directive 里加引导（当有子 agent 结果含 NEEDS_APPROVAL 时）
NEEDS_APPROVAL_GUIDE = """
A subagent returned [NEEDS_APPROVAL: keyword=X, tool=Y, args=Z] in its result.
This means the subagent tried to execute an operation containing a high-risk keyword
and was blocked. You (the main agent) have HITL capability — use the `question` tool
to ask the user whether to approve this operation. If approved, resume the TaskFlow
step with the approval so a new subagent can proceed.
"""
```

父 agent 的 model 读到后调 `question`：

```python
# 父 agent 调 question
question(
    questions=[QuestionSpec(
        question=f"子 agent 需要执行: terminal({args})，含高危关键字 '{keyword}'。是否允许？",
        options=[
            QuestionOption(value="approve", label="允许执行"),
            QuestionOption(value="reject", label="拒绝"),
        ],
    )]
)
```

→ HITL interrupt → 用户审批。

### 2.5 审批后 resume 带 approval 上下文

**文件**：`agent/tools/taskflow/tools/taskflow_resume.py` 或 `_retry.py`

用户 approve 后，父 agent 调 taskflow resume：

```python
# 父 agent 调
taskflow_step(action='resume', step_id='s1', step_outcome='approved',
              approval_context={'keyword': 'payment', 'tool': 'terminal'})
```

`step_spawn_kwargs`（`_retry.py:229-244`）传递 approval_context：

```python
def step_spawn_kwargs(step: dict, *, flow_id: str | None = None) -> dict:
    return {
        "output_schema": step.get("response_schema"),
        "functional_role": step.get("functional_role"),
        "step_model": step.get("step_model"),
        "step_timeout_seconds": step.get("step_timeout_seconds"),
        "flow_id": flow_id,
        "step_id": step.get("step_id"),
        "approval_context": step.get("approval_context"),  # 新增
    }
```

`dispatch_child`（`_dispatch.py`）传递到 spawn → `SpawnRequest` 加 `approval_context` 字段 → `child_agent_factory` 写入子 agent 的 state_register：

```python
# child_agent_factory.py 在构建子 agent 时
if request.approval_context:
    state_register_mem.set_state(child_session_id, "KEYWORD_APPROVALS",
                                 [{"keyword": request.approval_context["keyword"],
                                   "tool": request.approval_context["tool"]}])
```

新子 agent 的 ToolGuardrails 检查 `KEYWORD_APPROVALS` → 匹配 → 放行。

### 2.6 用户 reject

用户 reject → 父 agent 调 `taskflow_step(action='resume', step_id='s1', step_outcome='blocked')` → 步骤标记 blocked → 依赖此步骤的后续步骤也被 block。

---

## 3. 完整流程推演

### 情况 1：executor 子 agent 遇到高危关键字

```
main agent 创建 TaskFlow flow（部署任务）
  step 1: dispatch executor 子 agent

executor 子 agent spawn
  → 子 agent 调 terminal(curl POST /api/payment)
  → ToolGuardrails 扫描：含 "payment"（policy=block）
  → 检查 KEYWORD_APPROVALS：空（首次 spawn 无 approval）
  → 返回 error: "[NEEDS_APPROVAL: keyword=payment, tool=terminal]"
  → 子 agent model 看到错误，无法继续
  → 子 agent result_text: "部署被拦截。[NEEDS_APPROVAL: keyword=payment, tool=terminal, args=curl POST /api/payment]"
  → 子 agent 完成（TERMINAL）

announce pipeline 投递结果到 main agent
  → main agent model 读到 "[NEEDS_APPROVAL: keyword=payment...]"
  → main agent 调 question("子 agent 需要执行含 'payment' 的操作，是否允许？")
  → HITL interrupt → 用户审批

用户 approve:
  → main agent 调 taskflow_step(action='resume', step_id='s1', step_outcome='approved',
        approval_context={'keyword': 'payment', 'tool': 'terminal'})
  → step_spawn_kwargs 传 approval_context
  → dispatch_child spawn 新子 agent
  → child_agent_factory 写入 KEYWORD_APPROVALS=[{keyword:'payment', tool:'terminal'}]
  → 新子 agent 调 terminal(curl POST /api/payment)
  → ToolGuardrails 扫描：含 "payment"
  → 检查 KEYWORD_APPROVALS：匹配 → 放行 ✅
  → 执行成功
  → 完成 → announce 投递结果 → main agent 继续

用户 reject:
  → main agent 调 taskflow_step(action='resume', step_id='s1', step_outcome='blocked')
  → 步骤 blocked → 后续依赖步骤也 block
```

✅ 完整冒泡：子 agent 拦截 → 完成 → 父 agent 读结果 → question 问用户 → 审批 → resume 带 approval → 新子 agent 放行。

### 情况 2：main agent 自己遇到高危关键字

```
main agent 调 terminal(rm -rf /)
  → after_model → HighRiskKeywordApprovalHandler 检查
  → 含 "rm -rf"（内置关键字，policy=block）
  → HITL interrupt → 用户审批
  → approve → wrap_tool_call 放行（HITL 已审批）
  → reject → wrap_tool_call deny
```

✅ main agent 走 HITL handler，不冒泡。

### 情况 3：子 agent 遇到非关键字的高危操作（high_risk 工具 verify 重测）

```
子 agent 调 mcp__stripe__charge(amount=100)（无关键字命中）
  → ToolGuardrails 扫描：无关键字命中
  → 高危操作账本检查：hash 命中（之前执行过）
  → 返回 error: "[NON-IDEMPOTENT GUARD] 此操作已执行过"
  → 子 agent model 看到错误 → 用首次结果验证 → 继续
```

✅ 高危操作账本 block（不需要审批——用首次结果即可）。

### 情况 4：子 agent 遇到关键字 + 账本同时命中

```
子 agent 调 mcp__stripe__charge(card_number=4242..., amount=100)
  → ToolGuardrails 扫描：含 "card_number"（关键字命中）
  → 返回 [NEEDS_APPROVAL] error
  → 子 agent 完成 → 冒泡 → 父 agent question → 用户 approve
  → 新子 agent spawn 带 approval
  → 新子 agent 调 mcp__stripe__charge(card_number=4242..., amount=100)
  → ToolGuardrails: approval 匹配 → 放行
  → 高危操作账本检查：hash 命中（之前执行过）→ block
  → ⚠️ 问题：approval 放行了关键字，但账本仍 block
```

⚠️ **问题**：approval 放行关键字后，账本仍 block（防 verify 重测）。这是**正确行为**——用户审批了关键字风险，但 verify 重测的重复执行风险仍需防。用户如果要重跑，需要在前端删除账本记录。

### 情况 5：approval 不永久放行

```
子 agent step 1: terminal(curl POST /api/payment) → NEEDS_APPROVAL → 用户 approve
  → 新子 agent spawn 带 KEYWORD_APPROVALS=[{keyword:'payment', tool:'terminal'}]
  → 执行成功 → 完成

子 agent step 2: terminal(curl POST /api/payment) → 又遇到 "payment"
  → 检查 KEYWORD_APPROVALS：空（step 2 是新 spawn，不继承 step 1 的 approval）
  → 返回 [NEEDS_APPROVAL] → 再次冒泡 → 再次问用户
```

✅ approval 是 per-step 的——每个 TaskFlow step 的 spawn 独立审批。不永久放行。

### 情况 6：子 agent 不是 TaskFlow step（直接 spawn）

```
main agent 直接调 sessions_spawn（不走 TaskFlow）
  → 子 agent 遇到关键字 → 返回 NEEDS_APPROVAL → 完成
  → announce 投递结果到 main agent
  → main agent 读 [NEEDS_APPROVAL]
  → main agent 调 question → 用户 approve
  → main agent 重新调 sessions_spawn 带 approval_context
```

⚠️ **需要扩展 sessions_spawn 支持 approval_context**：和 TaskFlow 的 `step_spawn_kwargs` 一样，`SpawnRequest` 加 `approval_context` 字段。直接 spawn 的场景，main agent 手动传 approval。

---

## 4. 文件变更清单

| 文件 | 操作 | 行数 |
|------|------|------|
| `agent/middlewares/tool_guardrails/core.py` | wrap_tool_call 加关键字扫描 + KEYWORD_APPROVALS 检查 + NEEDS_APPROVAL error | ~60 |
| `agent/tools/subagent/spawn/request.py` | SpawnRequest 加 `approval_context` 字段 | ~5 |
| `agent/tools/subagent/spawn/core.py` | `_spawn_subagent` 传 approval_context | ~10 |
| `agent/tools/subagent/spawn/child_agent_factory.py` | 构建子 agent 时写 KEYWORD_APPROVALS 到 state_register | ~15 |
| `agent/tools/taskflow/tools/_dispatch.py` | dispatch_child 传 approval_context | ~5 |
| `agent/tools/taskflow/tools/_retry.py` | step_spawn_kwargs 加 approval_context | ~3 |
| `agent/tools/taskflow/tools/_shared.py` | _OPTIONAL_STEP_FIELDS 加 approval_context | ~1 |
| `agent/tools/subagent/tools/runtime_tools.py` | sessions_spawn 支持 approval_context 参数 | ~10 |
| `agent/middlewares/todo_continuation/core.py` | continuation directive 加 NEEDS_APPROVAL 引导 | ~15 |
| `tests/` | 冒泡流程 + approval 传递 + per-step 隔离 + reject 路径 | ~150 |
| **总计** | | ~274 |

---

## 5. 验收

| # | 场景 | 预期 |
|---|------|------|
| 1 | 子 agent 调含关键字工具 | ToolGuardrails 返回 [NEEDS_APPROVAL] error |
| 2 | 子 agent 完成后结果含 [NEEDS_APPROVAL] | announce 投递到父 agent |
| 3 | 父 agent 读到 [NEEDS_APPROVAL] → 调 question | HITL 审批对话框出现 |
| 4 | 用户 approve → 父 agent resume 带 approval | 新子 agent spawn 带 KEYWORD_APPROVALS |
| 5 | 新子 agent 调同工具 | ToolGuardrails 检查 approval → 放行 |
| 6 | 用户 reject → 父 agent resume blocked | 步骤标记 blocked |
| 7 | approval per-step 隔离 | step 2 不继承 step 1 的 approval → 需重新审批 |
| 8 | main agent 自己调含关键字工具 | HITL handler 直接拦截（不冒泡） |
| 9 | 子 agent 调 high_risk 工具但无关键字 | 账本 block（不走冒泡） |
| 10 | approval 放行后账本仍 block | 正确——approval 放关键字风险，账本防重复执行 |
| 11 | 直接 spawn（非 TaskFlow）+ 关键字 | sessions_spawn 带 approval_context |

---

## 6. Kill criteria

- 如果子 agent model 不在 result_text 里保留 [NEEDS_APPROVAL] 标记（LLM 忽略 error 里的指示），改为在 announce pipeline 里自动检测子 agent 的 tool_calls 历史中是否有 NEEDS_APPROVAL error → 自动注入到投递给父 agent 的消息里（不依赖 model 自觉）
- 如果 SpawnRequest 加 approval_context 导致 from_legacy 兼容问题（旧调用者不传），approval_context 默认 None → 不影响现有 spawn
- 如果 step_spawn_kwargs 加 approval_context 导致 _OPTIONAL_STEP_FIELDS 白名单问题，加 "approval_context" 到白名单
- 如果用户反复审批同一关键字（每个 step 都问），加 "session 级 approval"：用户 approve 后对整个 session 的该关键字+工具组合永久放行（用户在前端可撤销）

---

## 7. 和 Madorin 的对比

Madorin 的子 agent（Codex 原生子 agent）**完全没有 HITL 冒泡**——Codex 子 agent 的 HITL 只在 Codex 引擎内部处理，LinkSense 不介入。本计划的冒泡机制是 Sherry 独有的——利用现有的 announce pipeline + TaskFlow resume 实现子 agent→父 agent→用户的审批链。
