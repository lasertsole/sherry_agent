# 计划：Plan-Then-Execute 计划模式

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：sherry 有 TaskFlow（DAG 编排）和 HITL（逐操作审批），但没有"先出方案→用户审批→再执行"的计划模式。复杂任务（多文件改、架构决策、需求不明确）直接开跑容易走偏，用户无法在执行前审查和修正方向

## 对标

| 维度 | ZCode（金标准） | codex | oh-my-openagent | Octop | openclaw |
|---|---|---|---|---|---|
| 计划生成 | LLM 自由 markdown | LLM PlanItem | 专用 planner 子代理 + 固定模板 | 结构化课程记录 | skill_workshop proposal |
| 审批 | `ExitPlanMode` 工具显式审批 | 隐式（模式切换） | 隐式（展示给用户） | `confirm=True` 硬门 | apply/reject/quarantine |
| 工具限制 | **硬**——`checkPlanMode` 白名单只放只读 | 禁 plan tool；auto-turn 不能进 | prompt 级只读 + 单一写目标 | `plan_status=="ready"` 门 | mutation budget |
| 计划存储 | `.zcode/plans/plan-<id>.md` + 重新注入 | 对话项 | `.omo/plans/<slug>.md` + commit 引用 | track record | proposal revisions |
| 执行后偏移 | 软（提醒 only） | 无约束 | 软（模板自检） | 硬（有序执行） | 绑定到 revision |
| 状态机 | 无（boolean flag） | ModeKind enum | 无 | `needs_planning→ready→needs_replan` | proposal revision |

**借鉴策略**：
- 从 ZCode 取：`ExitPlanMode` 工具 + 硬权限白名单 + 计划文件持久化 + 4 阶段提醒
- 从 oh-my-openagent 取：固定计划模板（Scope/Acceptance/Verification/Final-wave）+ planner 子代理身份约束
- 从 Octop 取：`plan_status` 三态状态机（`needs_planning → ready → needs_replan`）
- 从 openclaw 取：mutation budget（防无限修订）+ revision binding（审批绑定到特定版本）

## 设计

### 核心机制：session 级计划模式 flag + HITL 审批门

sherry 已有的基础设施：
- **`ToolSelectionMiddleware`**——按 session 裁剪工具集（`request.override(tools=<subset>)`）→ 计划模式时只暴露只读工具
- **`HumanInTheLoop`**——逐操作审批 → 计划模式的审批门复用 HITL 的 interrupt 机制
- **`StateRegister`**——session 级状态存储 → 计划模式 flag 存这里
- **`workspace/sessions/<id>/plans/`**——已有的 plan 文件目录 → 计划持久化到这里
- **`TaskIntentMiddleware`**——已检测工作请求 → 可触发计划模式建议

### Plan Status 状态机（借鉴 Octop）

```
needs_planning ──(EnterPlanMode)──→ planning ──(ExitPlanMode + 用户审批)──→ ready
       ↑                                  │                                    │
       └──────────(replan)───────────────┘                                    │
                                                                              ↓
                                                                    needs_replan ──(replan)──→ planning
```

三态 + 两个迁移：
- `needs_planning` → `planning`：AI 调 `enter_plan_mode`
- `planning` → `ready`：AI 调 `exit_plan_mode`，HITL 审批通过
- `ready` → `needs_replan`：执行中发现需要改计划，AI 调 `enter_plan_mode`（replan）

存为 `StateKey.PLAN_STATUS`（新增到 `runtime/state_keys.py`）。

### 工具一：`enter_plan_mode`

```python
@tool("enter_plan_mode")
def enter_plan_mode(
    reason: str,           # 为什么需要计划（复杂度/架构/需求不明确）
    replan: bool = False,  # True = 对已审批的计划重新规划
    session_id: SessionId = "",
) -> str:
    """Enter plan mode. In plan mode, only read-only tools are available
    (read_file, search_files, message_search, ast_grep, code_intel, question).
    Explore the codebase, design the approach, then call exit_plan_mode with
    the plan for user approval. Use for: multi-file changes, architectural
    decisions, unclear requirements, multiple valid approaches."""
```

行为：
1. 设 `StateKey.PLAN_STATUS = "planning"`（或 `"needs_replan"` 如果 `replan=True`）
2. 注入计划模式系统提醒（4 阶段工作流，借鉴 ZCode）：
   - Phase 1：并行探索（spawn 只读子代理 / 用 search_files + read_file + code_intel）
   - Phase 2：设计（评估方案、选型、风险）
   - Phase 3：审查 + 澄清（用 `question` 工具问用户不确定的部分）
   - Phase 4：调 `exit_plan_mode` 提交计划
3. 提醒末尾："turns in plan mode may ONLY end with `question` or `exit_plan_mode`"
4. `ToolSelectionMiddleware` 的 `before_model` 检测 `PLAN_STATUS == "planning"` → `request.override(tools=<read_only_subset>)`

### 工具二：`exit_plan_mode`

```python
@tool("exit_plan_mode")
def exit_plan_mode(
    plan: str,              # 计划正文（markdown，≤ 20000 字符）
    allowed_actions: list[str] | None = None,  # 声明执行阶段需要的操作类别
    session_id: SessionId = "",
) -> str:
    """Submit the plan for user approval. The plan must be self-contained
    markdown covering: goal, scope (must-have / must-not), steps with
    acceptance criteria, verification strategy. After approval, plan mode
    exits and execution tools become available again."""
```

行为：
1. 持久化计划到 `workspace/sessions/<session_id>/plans/plan-<timestamp>.md`（原子写，借鉴 ZCode 的 `writeApprovedPlanFile`）
2. Stamp revision token（借鉴 openclaw）：`plan_revision = sha256(plan)[:16]`，存入 `StateKey.PLAN_REVISION`
3. 触发 HITL interrupt（复用 `HumanInTheLoop` 的 interrupt 机制）：
   - 审批内容 = 计划正文
   - 用户操作：批准 / 要求修改 / 拒绝
4. 用户批准 → `StateKey.PLAN_STATUS = "ready"` → 计划模式退出 → 注入退出提醒（"计划已批准，可以执行了"）+ 重新注入计划文件引用
5. 用户要求修改 → `StateKey.PLAN_STATUS` 保持 `"planning"` → 计划模式继续 → 注入用户反馈 → AI 修改计划后重新调 `exit_plan_mode`
6. 用户拒绝 → `StateKey.PLAN_STATUS = "needs_planning"` → 计划模式退出 → AI 决定是否重新规划

### 权限白名单（借鉴 ZCode 的 `checkPlanMode`）

`ToolSelectionMiddleware` 在计划模式下只暴露只读工具：

```python
PLAN_MODE_TOOLS = {
    "read_file", "search_files", "message_search",
    "ast_grep_search",  # 只读，不改
    "explore", "callers", "callees", "impact",  # code_intel 只读
    "question",        # 问用户
    "enter_plan_mode", "exit_plan_mode",  # 计划模式自身
    "sessions_spawn",  # 可以 spawn 只读子代理（Explore 角色）
}
```

`wrap_tool_call` 额外检查：即使工具名不在白名单（stale checkpoint 或幻觉），`PLAN_STATUS == "planning"` 时拒绝执行，返回 "Plan mode is active — only read-only tools are available. Call exit_plan_mode to submit your plan."

### 计划模板（借鉴 oh-my-openagent）

`exit_plan_mode` 的 `plan` 参数应遵循模板（在工具 description 里约束，不强制 JSON）：

```markdown
## Goal
<用户请求的一句话总结>

## Scope
- Must-have: <必须做到的>
- Must-NOT-have: <明确不做的边界>

## Steps
1. [step-1] <task> — depends on: none — accept: <验收标准>
2. [step-2] <task> — depends on: step-1 — accept: <验收标准>
...

## Verification
<如何验证整体完成：测试命令 / 类型检查 / 手动检查>

## Risk
<主要风险 + 缓解>
```

### 计划文件持久化 + 重新注入（借鉴 ZCode）

- 审批通过后，计划文件 `workspace/sessions/<id>/plans/plan-<timestamp>.md` 保留
- 后续 turn 的系统提示注入计划引用："A plan was approved at: <path>. Follow it. If the plan needs revision, call enter_plan_mode(replan=True)."
- 压缩后计划引用仍保留（计划文件在磁盘上，不依赖上下文窗口）
- 计划文件可被 `read_file` 重读（压缩后重读即恢复完整计划内容）

### Mutation Budget + Revision Binding（借鉴 openclaw）

- `StateKey.PLAN_MUTATION_COUNT`：每次 `exit_plan_mode` 被要求修改时 +1
- 超过上限（config，默认 5）→ 拒绝再修改，提示用户"计划已修改 N 次，请直接批准或拒绝"
- revision binding：用户审批绑定到特定 `plan_revision` token。如果 AI 在审批期间偷偷改了计划内容，token 不匹配 → 审批无效

### 与 TaskFlow 的衔接

计划审批通过后，AI 可以：
1. 手动调 `taskflow_create` + `taskflow_run_task` 逐步执行（当前方式）
2. 调 `taskflow_plan`（如果做了 TaskFlow 循环闭环计划的方案三）一键分解 → 自动注册 + dispatch
3. 计划模板里的 Steps 段落 → 可以被 `taskflow_plan` 解析为 step 列表

计划模式是"出方案 + 审批"，TaskFlow 是"执行方案 + 判定"——两者衔接在 `exit_plan_mode` 审批通过后。

## 改动清单

### 1. 新建 `agent/tools/plan_mode/` 工具模块

- `enter_plan_mode.py`：`EnterPlanModeTool`，设状态 + 注入提醒
- `exit_plan_mode.py`：`ExitPlanModeTool`，持久化计划 + HITL 审批 + 状态迁移
- `__init__.py`：`build_plan_mode_tools()` 工厂
- ~200 行

### 2. 修改 `agent/middlewares/tool_selection/core.py`

`before_model` 检测 `StateKey.PLAN_STATUS`：
- `"planning"` / `"needs_replan"` → `request.override(tools=PLAN_MODE_TOOLS)`
- `wrap_tool_call` 加计划模式拒绝检查（stale checkpoint 防线）
- ~30 行

### 3. 修改 `runtime/state_keys.py`

新增：
```python
PLAN_STATUS = "plan_status"           # needs_planning / planning / ready / needs_replan
PLAN_REVISION = "plan_revision"       # sha256(plan)[:16]
PLAN_MUTATION_COUNT = "plan_mutation_count"
PLAN_FILE_PATH = "plan_file_path"
```

### 4. 修改 `workspace/prompt_builder.py`

注入计划模式系统提醒（当 `PLAN_STATUS` 为 `planning`/`needs_replan` 时）：
- 4 阶段工作流提醒
- 计划模板
- "turns may ONLY end with question or exit_plan_mode"
- ~50 行

### 5. 修改 `agent/tools/__init__.py`

注册 `build_plan_mode_tools` 到 `_MAIN_TOOLS_BUILDERS`

### 6. 修改 `agent/tools/catalog.py`

加 `plan_mode` 组（不在 `REQUIRED_TOOLS`，不在 `BULK_ONLY_GROUPS`——可按 session 关闭）

### 7. 修改 `agent/middlewares/task_intent/core.py`

检测到复杂工作请求时，建议使用计划模式（注入提示："This task looks complex. Consider calling enter_plan_mode to propose a plan before executing."）。不强制——AI 自主决定。

### 8. 新建 `config/features/agent_side/plan_mode.py`

```python
class PlanModeConfig(TypedDict):
    enabled: bool               # True
    max_plan_chars: int         # 20000
    max_mutations: int          # 5（mutation budget）
    plan_mode_tools: list[str]  # 白名单工具名
```

### 9. 测试

- `test_plan_mode_enter_exit.py`：进入→提交计划→审批通过→退出→工具恢复
- `test_plan_mode_restriction.py`：计划模式下 write_file/patch_file/terminal 被拒绝
- `test_plan_mode_replan.py`：审批通过后→发现需要改→enter_plan_mode(replan=True)→重新提交
- `test_plan_mode_mutation_budget.py`：修改 6 次被拒
- `test_plan_mode_revision_binding.py`：审批期间计划被改→token 不匹配→审批无效
- `test_plan_mode_file_persistence.py`：计划文件写入→后续 turn 注入引用→压缩后仍可重读

## 验收

- 复杂任务：AI 调 `enter_plan_mode` → 探索代码 → 调 `exit_plan_mode` 提交计划 → 用户审批 → AI 执行
- 计划模式下 `write_file` / `patch_file` / `terminal` / `python_repl` 全被拒绝
- 审批后计划文件在 `workspace/sessions/<id>/plans/` 可见
- 后续 turn 系统提示包含计划引用
- 压缩后计划引用仍在（磁盘文件不依赖上下文）
- replan：执行中发现问题 → `enter_plan_mode(replan=True)` → 重新提交 → 重新审批
- mutation budget：修改超 5 次被拒

## 风险

- **AI 不主动用计划模式**：缓解——TaskIntentMiddleware 注入建议；工具 description 明确"何时用"；但不能强制（强制会导致简单任务也被拖慢）
- **审批 UX**：sherry 前端需要渲染计划 markdown + 批准/修改/拒绝按钮。HITL 已有审批 UI 基础——计划审批是更大粒度的审批
- **与 HITL 的关系**：计划模式是"执行前审批"，HITL 是"执行中审批"。两者不冲突——计划审批通过后，执行阶段仍可触发 HITL 逐操作审批（如果配置了）
- **计划偏离**：审批后 AI 执行时偏离计划——软约束（系统提醒注入计划引用），不做硬约束（硬约束会让执行卡死）

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/tools/plan_mode/enter_plan_mode.py` | 新建：进入计划模式工具 |
| `agent/tools/plan_mode/exit_plan_mode.py` | 新建：提交计划 + HITL 审批 |
| `agent/middlewares/tool_selection/core.py` | 修改：计划模式工具白名单 |
| `runtime/state_keys.py` | 修改：加 4 个 plan 状态键 |
| `workspace/prompt_builder.py` | 修改：注入计划模式提醒 + 计划引用 |
| `agent/tools/__init__.py` | 修改：注册 plan_mode 工具 |
| `agent/tools/catalog.py` | 修改：加 plan_mode 组 |
| `agent/middlewares/task_intent/core.py` | 修改：建议计划模式 |
| `config/features/agent_side/plan_mode.py` | 新建：配置 |
