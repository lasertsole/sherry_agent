# 🛡️ TodoList 强制执行层 — E1–E7 与编排纪律

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [TodoList](../README.zh.md) 的一部分：让编排者持续规划、委派、验证与诚实的七层强制执行（E1–E7）。

---

## 强制执行层 E1–E7

### E1: 系统提示词强制 — orchestrator doctrine

添加到 `AGENTS.md`，零代码纯文本：

```markdown
## Task Management & Orchestration (CRITICAL)

### Orchestrator Doctrine (MANDATORY)

YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.

- You DO NOT write code. You DO NOT edit product files.
- EVERY unit of implementation MUST be delegated to a spawned subagent.

### When to Create Todos (MANDATORY)

- Multi-step task (2+ steps) → ALWAYS create todos first
- Uncertain scope → ALWAYS (todos clarify thinking)

### Workflow (NON-NEGOTIABLE)

1. IMMEDIATELY on receiving request: todowrite to plan atomic steps.
2. For each checkbox: decompose into atomic sub-tasks for ONE worker.
3. DELEGATE every sub-task via task tool — route by category.
4. Before starting each step: Mark in_progress (only ONE at a time)
5. After subagent returns: verify via acceptance criteria, then mark completed.

### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Do NOT mark a TaskFlow-linked todo completed while its step is not `done`

### Completion Contract (Sisyphus)

- DoneClaim → AdversarialVerify → FullyDone
- If verification fails, the task is NOT done — re-dispatch or fix.

**FAILURE TO FOLLOW ORCHESTRATOR DOCTRINE = INCOMPLETE WORK.**
```

### E2: 工具描述强制 — 格式 + 委派规则

在 `build_todolist_tools()` 中覆盖 todowrite 描述，注入 MANDATORY 格式规则：

- 标题编码 WHERE/WHY/HOW/RESULT
- 原子粒度（1-3 次工具调用）
- 同时只有一个 in_progress
- 不得在 subagent 返回前标记 completed（转换屏障）
- 不得在未验证前标记 completed（Sisyphus）

### E3: 续作强制器 — 事后闭环核心 ★★

turn 结束后如果有未完成 todo，系统自动注入续作消息把 LLM 拉回来。不依赖模型自觉。

**关键常量**：

- `_MAX_STAGNATION = 3`（连续 3 次无变化 → 停止）
- `_BASE_COOLDOWN_S = 2.0`（基础退避）
- `_MAX_COOLDOWN_S = 60.0`（最大退避）
- `_FAILURE_RESET_WINDOW_S = 300`（5 分钟无失败 → 重置）
- `_MAX_RECOVERY_ATTEMPTS = 2`（恢复模式上限）

**工作流程**：

```
turn ends (no tool_call, agent loop exits)
  → Summarization.aafter_agent (rebuild system prompt with latest todos)
  → TodoContinuationEnforcer.aafter_agent
      → is_abort_error? → skip
      → get_todos_sync(session_id) → filter incomplete
      → check_stagnation: snapshot compare, N consecutive no-change?
          → should_enter_recovery? → RECOVERY_PROMPT
          → else: stop continuation
      → is_in_cooldown? → skip
      → build continuation prompt (full todo status + flow/step)
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _watch_user_takeover() 0.5s poll
      → user sends message → detect_state() busy → cancel → reset()
```

**文件**：`stagnation_tracker.py`（~90 行）+ `todo_continuation/core.py`（~110 行）+ `agent/core.py`（~2 行注册）。

### E4: 转换屏障 — 双保险

**Prompt 级**（E1 AGENTS.md）：subagent 运行中 / TaskFlow step 未 done 时不得标 completed。

**代码级硬阻断**（`service.py`），两个来源都不在 todolist 内建调度器：

1. **TaskFlow step 状态**：todo 关联 `flow_id`/`step_id` → 只有 `done` 允许标 `completed`，`blocked`/`ready`/`dispatched` 都阻断。
2. **subagent registry 存活检测**：todo 关联 `subagent_id` → `get_run_by_child_session_key` + `is_live_unended_run` 判断子会话是否仍在运行。

```python
for todo in validated:
    if todo["status"] != "completed":
        continue
    # 来源 1：TaskFlow step 状态
    flow_id, step_id = todo.get("flow_id"), todo.get("step_id")
    if flow_id and step_id:
        step_status = _read_taskflow_step_status(flow_id, step_id)
        if step_status is not None and step_status != "done":
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} is '{step_status}'. "
                "Call taskflow_wait_all then taskflow_resume to inject the result first."
            )
    # 来源 2：subagent 存活检测
    if todo.get("subagent_id") and _is_subagent_running(todo["subagent_id"]):
        raise TodoStoreError(
            f"Cannot mark todo completed: subagent {todo['subagent_id']} is still running."
        )
```

### E5: Sisyphus 完成契约 ★★

三阶段完成验证，对"虚假完成"的最终防线：

```
Worker 返回 → DoneClaim → AdversarialVerify（5 gates）→ FullyDone / NOT done
  Gate 1: Plan reread（重读计划，确认验收标准）
  Gate 2: Automated verification（运行验证命令）
  Gate 3: Manual QA（人工或 agent 检查）
  Gate 4: Adversarial QA（对抗性检查：stale state / dirty worktree / leftover resources）
  Gate 5: Cleanup（清理临时资源）
```

**文件**：`verifier.py`（~80 行）+ `subagent_completion_drain/core.py` 扩展（~15 行，追加验证提醒）。

### E6: 委派路由 ★

**生命周期**：

```
LLM creates todo (with category + delegation fields)
  → #a fan-out reminder (first time per session)
  → #b category tells LLM which subagent type to spawn
  → #c AGENTS.md guides how to delegate
  → Dependent steps: taskflow_run_task(depends_on=[...]) → blocked or dispatched
  → Ready steps: taskflow_dispatch(flow_id, step_ids) → get child_session_key
  → LLM updates todo's subagent_id + flow_id/step_id
  → #d-prompt: "Do NOT mark done before step done / subagent returned"
  → #d-code: update_todos() checks step status + is_live_unended_run() → hard block
  → taskflow_wait_all → taskflow_resume → E5 Sisyphus verify → LLM marks completed
```

**Category 路由表**：

| Category     | 路由到                           | 说明               |
| ------------ | -------------------------------- | ------------------ |
| `quick`      | subagent spawn (default model)   | 机械、单文件、样板 |
| `deep`       | subagent spawn (reasoning model) | 复杂调试、跨模块   |
| `ultrabrain` | subagent spawn (最强 model)      | 困难逻辑问题       |
| `visual`     | subagent spawn                   | 前端、UI/UX        |
| `git`        | subagent spawn                   | git 操作           |
| `writing`    | subagent spawn                   | 文档               |

### E7: 意图识别与引导 — 事前触发器 ★★

双模式设计，解决"模型不自觉调用计划工具"：

| 场景                    | 模式           | 行为                      |
| ----------------------- | -------------- | ------------------------- |
| 首次任务请求，无计划    | E7a            | 注入完整引导 prompt       |
| 已 armed + 新任务       | E7a 轻量       | 注入短提醒                |
| 有活跃计划 + 用户发消息 | E7b            | 追加 plan-active reminder |
| 有活跃计划 + turn 结束  | (E3)           | E7 不干预                 |
| 非任务（问答/闲聊）     | 跳过           | 不注入                    |
| 压缩后 + 新任务         | E7a 重新 armed | 重新注入完整引导          |

**意图检测**：轻量级启发式，不调用 LLM（零延迟、零成本）——问答模式 → 非任务；闲聊模式 → 非任务；任务关键词 → 任务；长消息非问答 → 可能是任务。

**防循环设计**：E7b 优先于 E7a；E7a 每会话一次（`_armed_sessions` Set；已 armed 则只发短提醒）；E7b 只在 `before_model` 运行（E3 的 `after_agent` 续作不会触发 E7b）；`_is_system_directive()` 过滤系统注入消息（E7 注入带 `metadata={"origin":"task_intent","internal":true}`，存储层与过滤器可正向识别为内部消息，绝不当作真实用户请求，也不会进入会话标题或用户请求提取）；E3 有退避冷却；压缩后 re-arm。

**文件**：`agent/middlewares/task_intent/core.py`（~160 行）+ `agent/core.py`（~2 行注册）。
