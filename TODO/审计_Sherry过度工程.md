# Sherry 过度工程审计

- 建档日期：2026-10-14
- 状态：审计完成，基于源码逐条核验
- 方法：对每个候选项读实际源码（非 AGENTS.md 描述），验证成本、触发条件、是否有实际调用者

---

## 0. 总览

| # | 候选 | 判定 | 核心理由 |
|---|---|---|---|
| 1 | CompletionJudge always-on | **过度** | 每次 spawn 付费 ≥1 辅助 LLM 调用，简单 spawn 不需要 |
| 2 | SisyphusVerifier (Gate D) | **过度** | 248 行模块，零生产调用者，仅测试 monkeypatch |
| 3 | TaskIntent 两层检测脚手架 | **过度** | ≤1 LLM 调用/session 但周边机制比它守护的东西更重 |
| 4 | Gate E ∩ CompletionJudge 重叠 | **过度** | 两个 LLM judge 都问"目标是否达成"，一个在 child 级一个在 flow 级 |
| 5 | PlanContinuationEnforcer | **过度** | 自主多步场景合理；用户在场时多余。可 per-session 关闭，但默认开 |
| 6 | ReminderChannel 枚举 (6 取 1) | **轻微** | 前瞻抽象无消费者，不伤运行时 |
| 7 | 四个 loop/stall 守卫 | **不过度** | 各管不同失败模式（wall-clock / 文本循环 / 迭代数 / 工具循环），非冗余 |
| 8 | MaxTokensBoost | **不过度** | 不是思考相关，是 tool-call JSON 截断恢复，按需付费 |
| 9 | SubagentCompletionDrain | **不过度** | 消息注入正确性保证，纯 Python，可关闭 |
| 10 | DynamicReminderMiddleware | **不过度** | 30K 是天花板非实际注入，change-detection + cooldown 使实际 ≪ 30K |
| 11 | TaskFlow 16 工具 | **不过度** | 全部注册可达，无死代码；"过度"的质疑是概念层面不是代码层面 |
| 12 | MultimodalProcessor | **不过度** | 真实工作（媒体持久化 + native/skill 路由），非 pass-through |

---

## 1. CompletionJudge：每次 spawn 都跑辅助 LLM 判定（过度）

### 1.1 事实

- **文件**：`agent/tools/subagent/spawn/completion_judge.py`、`agent/tools/subagent/spawn/core.py:853-890, 1119-1124`
- **触发**：`if goal_max_turns > 1:` (`core.py:1119`)，默认 `goal_max_turns=5`（`config/features/agent_side/completion_judge.py:22`），所以**每次 spawn 都进入 goal loop**
- **第一轮就调 judge**：`turns_used` 从 1 开始，judge 调用 (`core.py:872`) 在预算检查 (`:879`) 之前
- **成本**：最少 1 次辅助 LLM 调用/spawn（即使第一轮就 DONE）；最坏 5 次 judge + 4 次额外子 agent turn
- **fail-open**：任何错误/解析失败降级为 DONE (`completion_judge.py:139-140, 168-170`)
- **无开关**：config 只暴露 `goal_max_turns`（turn 预算），没有 on/off flag。唯一跳过方式是设 `goal_max_turns=1`

### 1.2 为什么过度

简单 spawn（"读这个文件""搜这个关键词"）的结果本就明确，再花一轮 LLM 调用判定"目标是否达成"是纯开销。ZCode 的做法是程序化终态等待——等子 agent 跑完就完事，不额外跑 judge。

### 1.3 建议

- 加一个真正的 enable flag（不是 `goal_max_turns=1` 的 workaround）
- 或：当子 agent 第一轮就自报完成且有 passing evidence 时，跳过 judge
- 或：让 spawn 工具的 caller 声明是否需要 completion judging（`judge_completion=False`），默认对 executor 角色开、对 researcher 角色关

---

## 2. SisyphusVerifier (Gate D)：完整门控零生产调用者（过度）

### 2.1 事实

- **文件**：`agent/tools/taskflow/tools/taskflow_finish.py:138-167, 243-246`、`agent/tools/todolist/verifier.py`（248 行）
- **触发条件**：caller 同时传 `todo` + `plan_path` 给 `taskflow_finish` (`:151: if not todo or not plan_path: return None`)
- **调用者搜索**：全仓库 grep `todo=` + `plan_path=` 传给 `taskflow_finish` —— **唯一调用者在 `tests/`**（`test_finish_gate.py`，且 monkeypatch 了 `SisyphusVerifier.verify`）。**零生产调用路径**
- **成本**：纯代码（无 LLM）。但 248 行模块（注入 seam、5-gate 契约、ledger 追加）的维护成本不为零
- **docstring 告诉模型"可以"传这两个参数，但无 prompt 强制要求、无自动 linkage 填充**

### 2.2 为什么过度

一个完整构建的门控——注入 seam、5-gate 契约、EvidenceLedger 追加——为一个实际不发生的调用路径服务。它不是死代码（模型可以选择传参），但在实践中是休眠的。

### 2.3 建议

- 选项 A：自动 wire linkage（让 `taskflow_finish` 自动从 flow 的 step 中提取 `todo` + `plan_path`），使 Gate D 实际运行
- 选项 B：标记为 deprecated，如果确认无生产调用者则删除 248 行模块
- 保留 guard（`if not todo or not plan_path: return None`）本身成本为零，不急

---

## 3. TaskIntent 两层检测 + arming + boulder-probe 脚手架（过度）

### 3.1 事实

- **文件**：`agent/middlewares/task_intent/core.py`
- **两层**：
  - 层 1（规则）：4 语关键词 (EN/ZH/JA/KO) + question/chat 模式 + 长度 >100 字符 fallback。零成本
  - 层 2（LLM）：**仅首次 arming**、仅规则无结论时、仅 20-100 字符窗口、仅带请求措辞（`してください`/`해줘`/`please`/`帮我`）。≤1 辅助 LLM 调用/session，fail-open
- **周边机制**：per-session arming ledger (`_armed_sessions`)、plan-active boulder 文件探测 (`_has_active_boulder`)、re-arm-after-compact hook
- **可关闭**：在 `GATEABLE_MIDDLEWARES` 中，per-session 可禁用

### 3.2 为什么过度

LLM 调用本身是可辩护的（bounded、fail-open、≤1/session）——触发窗口极窄（短 JA/KO 请求无关键词但带请求措辞），正是关键词列表漏掉的形状。但**周边脚手架**（两层检测 + arming ledger + boulder 文件探测 + rearm-after-compact）比它守护的东西（决定是否进入 orchestrator/plan mode）更重。

### 3.3 建议

- 保留层 1（规则），砍掉层 2（LLM fallback）——关键词 + 长度 fallback 已覆盖大多数场景
- 或：保留层 2 但移除 boulder 文件探测 + rearm-after-compact，简化为"首次 arming 用 LLM，之后规则 only"

---

## 4. Gate E (goal judge) ∩ CompletionJudge 重叠（过度）

### 4.1 事实

- **CompletionJudge**（`spawn/completion_judge.py`）：child 级——每次 spawn 用辅助 LLM 判定子 agent 的目标是否达成
- **Gate E**（`taskflow_finish.py:251`，`_goal_gate`）：flow 级——`taskflow_finish` 时如果 flow 有 `description` 且 `GOAL_GATE["enabled"]=True`，用辅助 LLM 判定收集的结果是否满足 flow 目标
- **两个都是 LLM judge，都问"目标是否达成"**，一个在 child 级一个在 flow 级

### 4.2 为什么过度

如果 CompletionJudge 已经在每个子 agent spawn 时判定了目标达成，Gate E 在 finish 时再判一次 flow 级的目标——这是两层 LLM judge 审查同一件事的不同粒度。对于简单 flow（1-2 个 step），两层 judge 重叠度高。

### 4.3 建议

- Gate E 是 opt-in（`GOAL_GATE["enabled"]=True`），目前默认关。如果确认默认关且无路径开启，与 Gate D 同类问题
- 如果两者都开：考虑让 Gate E 只在 flow 有 ≥3 个 step 时才跑（简单 flow 信任 CompletionJudge 的 child 级判定）

---

## 5. ReminderChannel 枚举：6 个 channel 仅 1 个 live（轻微）

### 5.1 事实

- `agent/middlewares/reminder_catalog/sources.py` 注册 7 个 source，全部 `ReminderChannel.CURRENT_TURN`
- `core.py:101-107` 的 `register()` **拒绝**非 `CURRENT_TURN` channel
- 枚举定义了 6 个 channel（`SYSTEM_PROMPT` / `CURRENT_TURN` / `TOOL_RESULT` / `HISTORY_CONTINUITY` / `MID_TURN_EVENT` / `REAL_USER`），但只有 `CURRENT_TURN` 可消费

### 5.2 判定

轻微——前瞻抽象无消费者，不伤运行时（枚举本身零成本）。但如果长期只有 1 个 channel，枚举的 6 值是认知负担。

### 5.3 建议

最低优先级。要么激活其他 channel（如 `TOOL_RESULT` 用于工具结果内注入），要么精简枚举为只有 `CURRENT_TURN`。

---

## 6. PlanContinuationEnforcer：复杂且用户在场时不必要（过度）

### 6.1 事实

- **文件**：`agent/middlewares/todo_continuation/core.py`
- **做什么**：turn-end (`aafter_agent`) 时，如果 session 有未完成 todo 或非终态 TaskFlow flow，构建 continuation directive 并通过 `runtime.hooks.MAYBE_TRIGGER_AUTO_TURN` fire-and-forget 注入下一个 idle turn
- **有 guard**：blank session → no-op；abort-class error → no-op；无未完成项 → no-op；stagnation → recovery/give-up；cooldown → skip
- **stagnation tracker 不是替代品**——它是此 middleware 的子组件（`record_turn` / `check_stagnation` / `enter_recovery`），无独立触发。10-turn `[REMINDER: TODO TRACKING]` 是追加到同一 directive 里，不是独立机制
- **可关闭**：在 `GATEABLE_MIDDLEWARES` 中

### 6.2 为什么过度

如果用户在场（典型个人 agent 场景），agent 应该停下来等用户反馈而不是自动延长 turn。auto-turn 延长在自主多步场景（用户发完消息就走了）有道理，但大多数交互场景下是多余的——agent 自然会继续工作，不需要 fire-and-forget auto-turn 触发器。stagnation tracker 的 10-turn 提醒如果作为独立轻量机制可能就够了，但它被绑在了这个复杂 middleware 里。

### 6.3 建议

- 内置预设中：情感陪伴 / 纯净（用户在场场景）默认关 PlanContinuationEnforcer；编程助手 / 全量（可能自主场景）默认开
- 或：拆分——stagnation tracker 独立为轻量 reminder（10-turn `[REMINDER: TODO TRACKING]`），auto-turn 延长作为可选高级特性
- **enforcer 已开启时的限界**：见 `计划_续跑enforcer加turn级硬上限.md` §3（in-memory turn 计数器，非 LLM 机制，与本文"过度工程"结论不冲突——审计管"何时开 enforcer"，该计划管"开了之后如何限界"）

---

## 7. 确认不过度的项

### 7.1 四个 loop/stall 守卫（看起来冗余实际不是）

| 守卫 | 失败模式 | 为什么不可互相替代 |
|---|---|---|
| IterationBudget | 迭代数超限（90 次模型+工具调用） | 唯一的优雅停止机制；langgraph `recursion_limit` 未设，默认 25 graph steps 语义不同且抛 raw RecursionError |
| ToolGuardrails | 工具调用循环（exact-failure / ping-pong / arg-churn / no-progress） | 管工具调用模式 |
| OutputRepetitionGuard | 文本输出循环（无工具调用时的文本死循环） | 管文本输出模式，与 ToolGuardrails 互补 |
| HeartbeatStaleness | wall-clock 无进展（挂起工具 / API stall） | 一个工具调用挂住永远不增迭代数，IterationBudget 永不触发。HeartbeatStaleness 是 wall-clock 看门狗 |

各管不同失败形状，非冗余。

### 7.2 MaxTokensBoost

- **不是思考相关**——源码中无任何 thinking 引用。触发条件是 `finish_reason in {"length","max_tokens"}` 且响应带 `tool_calls`（tool-call JSON 被截断）
- 按需付费（只在截断时重调），比总是预留 headroom 更省
- **不过度**

### 7.3 SubagentCompletionDrainMiddleware

- 纯 Python（无 LLM），注入排队的 completion 消息（正确性保证）
- 可 per-session 关闭（`GATEABLE_MIDDLEWARES`）
- 嵌入的 verification gate 调用 `collect_evidence_summary`（无 LLM），只追加静态字符串
- 与 Gate C 有轻微重叠（不同 scope：parent 侧 vs flow 侧），可辩护
- **不过度**

### 7.4 DynamicReminderMiddleware

- 7 个 source，全部 `CURRENT_TURN` channel
- 30K 是天花板不是实际注入——每个 source 有 `only_if_changed=True`（hash 对比跳过未变）+ 多个有 `cooldown_turns=5`
- 配置注释自己说"实测最坏 ~15K"，≈4K token，占 128K 窗口 ~3%
- **不过度**

### 7.5 TaskFlow 16 工具

- 实际是 16 个（不是 AGENTS.md 说的 14），全部在 `TOOL_GROUPS` 的 `tasks` 组注册可达
- `BULK_ONLY`（全开/全关），无单独死工具
- "过度"的质疑是概念层面（个人 agent 是否需要 DAG 编排），不是代码层面

### 7.6 MultimodalProcessor

- 真实工作：`before_agent` 验证/持久化上传媒体 + 决定 native/skill 路径 + 清理历史 stale `image_url`；`wrap_model_call` per-request scrub；`after_agent` 清理 temp 目录
- 文件 I/O off-loop（`asyncio.to_thread`）
- **不过度**

---

## 8. 行动优先级

| 优先级 | 行动 | 理由 |
|---|---|---|
| P0 | CompletionJudge 加 enable flag + "第一轮自报完成跳过 judge"路径 | 每次 spawn 都花的真金白银 token |
| P0 | SisyphusVerifier：确认无生产调用者后标 deprecated 或自动 wire linkage | 248 行休眠模块 |
| P1 | PlanContinuationEnforcer：用户在场场景默认关；或拆分 stagnation tracker 为独立轻量机制 | auto-turn 延长在交互场景下多余，agent 自然会继续 |
| P1 | TaskIntent 砍层 2（LLM fallback）或砍周边 boulder-probe/rearm 脚手架 | 机制比守护对象更重 |
| P1 | Gate E 与 CompletionJudge 去重叠（简单 flow 信任 child 级判定） | 两个 LLM judge 问同一件事 |
| P3 | ReminderChannel 枚举精简或激活其他 channel | 前瞻抽象无消费者，零成本但认知负担 |
