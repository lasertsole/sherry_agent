# Plan-Then-Execute 计划模式

[English](README.md) · **中文** · [日本語](README.ja.md) · [한국어](README.ko.md)

会话可以先设计、再执行：Agent 进入**计划模式**，只用只读工具工作，提交计划，在用户批准之前不写任何文件。直接冲向多文件改动正是容易走偏的路径，而用户连纠偏的机会都没有。

事实来源：`agent/tools/plan_mode/`（两个工具 + 共享状态助手）、`agent/middlewares/tool_selection/core.py`（白名单）、`workspace/prompt_builder.py::_build_plan_mode_block`（系统提醒）、`config/features/agent_side/plan_mode.py`（可调参数）。

## 流程

```
                  enter_plan_mode
   needs_planning ────────────────► planning ──────────────► ready
        ▲                              │  ▲                   │
        │        reject (no message)   │  │ reject (message)  │ enter_plan_mode(replan=True)
        └──────────────────────────────┘  └─── revise ────────┘
                                                               │
                                            needs_replan ◄─────┘
                                                  │  (exit_plan_mode + 审批)
                                                  └──────────► ready
```

1. `enter_plan_mode(reason)` —— Agent 判断任务值得先出方案（多文件改动、架构决策、需求不明确、存在多种可行方案）。会话的计划状态变为 `planning`，工具面收窄到只读白名单。
2. Agent 探索（`read_file`、`search_files`、`message_search`、技能查看器），有歧义时用 `question` 问用户，然后完成设计。计划模式下的回合只能以 `question` 或 `exit_plan_mode` 调用结束。
3. `exit_plan_mode(plan)` —— 把计划持久化到 `workspace/sessions/<session_id>/plans/plan-<revision>.md`，并挂起图等待审批（聊天里显示计划，带 **批准** 与 **退回修改**）。
4. **批准** → 状态 `ready`；计划保留为会话的参照（系统提示词每轮都点名该文件），写工具恢复可用。**退回修改**（reject + 反馈）→ 反馈回传给 Agent，Agent 修改后重新提交。**拒绝**（无反馈）→ 计划被放弃，由 Agent 决定是否重新规划。

## 只读保证

状态为 `planning` / `needs_replan` 时，`ToolSelectionMiddleware` 会用白名单做两道拦截，和它原有的按会话工具选择完全一致：模型只会被**提供**白名单内的工具；而仍然到达的调用（旧 checkpoint、幻觉出的名字）会在执行处被**拒绝**，回执为 "Plan mode is active"。默认白名单在 `config/features/agent_side/plan_mode.py::PLAN_MODE["allowed_tools"]`；它依旧受会话自身工具选择的约束，唯独两个计划工具永远可达——一个无法离开的模式等于陷阱。

子代理这唯一的漏洞被刻意堵上：白名单里**没有** `sessions_spawn`——子代理以自己的会话状态运行，被派出的执行者可能在计划批准前就写文件。

## 守卫

* **修改预算** —— `PLAN_MODE["max_mutations"]`（5）：每个计划最多 5 轮"退回修改"。超出后 `exit_plan_mode` 拒绝再提交，要求 Agent 请用户直接给出批准或拒绝。
* **修订绑定** —— 审批绑定 `sha256(plan)[:16]`。若审批期间存储的修订号或计划**文件**发生变化，审批作废，计划必须重新提交。
* **大小上限** —— `PLAN_MODE["max_plan_chars"]`（20000）；超长计划被拒绝并提示"压缩一下"，而不是截断。
* **关闭开关** —— `PLAN_MODE["enabled"] = false` 时两个工具都不注册，其余行为不变。

## 用户看到的

计划以审批卡片出现在输入槽位（HITL 已有的同一处界面）：计划正文、反馈输入框、**退回修改**、**批准**。批准后回合继续，执行工具可用；要求修改则带着反馈继续，Agent 修改计划。

## 与其他门的关系

计划模式是"执行前审批"；HITL 是"执行中审批"（执行开始后，它照旧拦截原本拦截的操作）。TaskFlow 是"审批后执行"：计划里的编号步骤可以登记为 flow 步骤或 todo，由完成门禁判定结果。
