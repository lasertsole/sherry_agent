# 🧱 稳定系统提示词与提醒层

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Context Governance](../README.zh.md) 的一部分：为何系统提示词只承载稳定内容，而所有实时视图在每轮首个模型调用时以 `<system-reminder>` HumanMessage 注入。

---

系统提示词只承载**稳定**内容：persona 文件、memory 块与技能索引。所有**实时**视图——计划模式、工作目录、项目指令（`AGENTS.md`，回退 `CLAUDE.md`）、todo 列表、活动工作指针、TaskFlow 看板、连续性上下文——改为在每轮首个模型调用时以 `<system-reminder source="…">` HumanMessage 注入（`DynamicReminderMiddleware` + `agent/middlewares/reminder_catalog/` 目录）。最低优先级的 `current_time` 来源在同一块里每轮加入本地时钟：一行英文，例如 `Current local time: 2026-11-06 09:41:07 (Friday) (CST, UTC+08:00)`；它设 `transient=True`，上一轮的时间行会被剥离而不是累积；且刻意不用 `only_if_changed`——时钟必须前进。两个原因：首个 token 每轮都变的提示词会让提供方的前缀缓存从那里起全部失效（persona 也被连累）；而只在缓存未命中时重建的提示词，在下一次重建之前展示的是**过期**的 todo 列表。

冻结的记忆快照有一个刻意的例外。切换会话模型会进入新的提供方缓存命名空间；切换思考模式经实测在同一前缀上把 `cache_read` 打到 0——两种情况下缓存本来就要冷。`ThinkingControlMiddleware` 的 `before_model` 钩子比较本轮 `(thinking key, profile fingerprint, mapping fingerprint)` 与上次实际应用值（进程级保存，因为图每轮重建），变化时在 `system_prompt_injection` 之前重载 `MemoryStore` 快照并从两个寄存器删除 `StateKey.SYSTEM_PROMPT`。切换后的首轮首个模型调用因此能看到会话中途写入的记忆条目（底层是 SQLite；遗留 Markdown 文件只在启动时导入一次）。首轮、控制未变、重载失败都保留原缓存；没有发送就不会触发。

FACTS.md 是全局文件，目录切换不再使缓存提示词失效。同一轮的 `project_instructions` 提醒仍会重新读取新项目的 `AGENTS.md`（回退 `CLAUDE.md`），并靠 `only_if_changed` 每份不同内容只注入一次。

| 关注点 | 规则 |
|---|---|
| 注入什么 | 每个 builder 返回文本的来源各注入一条提醒：预算顺序为高→低，投递时反转为低→高，使最高优先级位于尾部；标记 `origin=<来源 id>`、`provenance="reminder_catalog"`、`internal=True` |
| 预算 | 每次调用全部来源合计 `REMINDER_CATALOG["total_budget_chars"]`（30 000）——超预算时整条丢弃优先级最低者，绝不把一条提醒截成半句 |
| 节流 | 来源可声明 `cooldown_turns`（TaskFlow 看板 / boulder：5）与 `only_if_changed`（内容相同不重复注入）；节流状态只为**真正注入过**的来源推进 |
| 压缩之后 | 压缩会重置目录，于是下一轮从 live state 重新注入每个来源——提醒层不依赖历史是否幸存 |
| 转义 | 载荷内嵌套的 `<system-reminder>` 标签（todo 文本可被用户影响）会被转义，否则载荷可提前闭合标签、冒充这一层 |
| 轮内定位 | 只在每轮首个模型调用注入——与 TaskIntent 相同的「最后一条消息是真实用户消息」判定；遍历人类消息的扫描器（`split_into_turns`、HITL 无操作者判定、摘要标签）通过 `pub.func.message.system_reminder` 跳过提醒 |

todo 列表还多一道保险：`PlanContinuationEnforcer`（由 `TodoContinuationEnforcer` 更名；它监督的"计划"= todos + TaskFlow flows）统计未调用 `todowrite` 的轮数，超过 `TODOLIST_INFRA["todo_write_nudge_turns"]`（10）就在续跑指令后追加 `[REMINDER: TODO TRACKING]` 提醒——这是基于快照的停滞检测唯一看不见的形态（模型推进 TaskFlow 会让 streak 清零，而 todo 列表在腐烂）。

## 📋 已注册来源

目录注册了九个来源（`agent/middlewares/reminder_catalog/sources.py::register_default_sources`）。它们都走同一通道（`CURRENT_TURN`），除 `current_time` 与 `memory_recall` 外都带 `only_if_changed=True`——载荷与上次注入**逐字节相同**的来源**不会**重复注入，即使冷却已过。目录**每轮只解析一次，仅在首个模型调用**（最后一条消息是真实用户消息时）；已恢复的轮次若在其用户消息前已带提醒块，则跳过重复注入。

| 来源（`source_id`） | 优先级 | 预算（字符） | 冷却 | Transient | 注入内容 |
|---|---|---|---|---|
| `memory_recall` | 88 | 4 000 | 3 轮 | No | 与当前请求相关的语义记忆命中（仅开关开启时） |
| `plan_mode_block` | 85 | 2 000 | 0 —— 每轮 | No | 计划模式状态/指令（agent 是否处于计划模式） |
| `todo_block` | 80 | 1 500 | 0 —— 每轮 | No | agent 当前的 todo / 工作计划列表 |
| `taskflow_status` | 75 | 2 000 | 5 轮 | No | TaskFlow 看板状态（被引导的看板） |
| `boulder_status` | 70 | 1 000 | 5 轮 | No | Boulder / 活动工作指针 |
| `continuity_context` | 65 | 1 000 | 0 —— 每轮 | No | 跨轮次的连续性上下文 |
| `project_instructions` | 62 | 8 000 | 0 —— 每轮 | Yes | 项目的 `AGENTS.md`（回退 `CLAUDE.md`） |
| `project_dir_notice` | 60 | 500 | 0 —— 每轮 | No | 工作目录通知 |
| `current_time` | 10 | 120 | 0 —— 每轮 | Yes | 本地时钟，英文格式：日期、星期、秒、UTC 偏移 |

**如何理解频率。** `cooldown_turns = 0` 表示*每轮都符合条件*——只要内容自上次注入后发生了变化就重新注入；内容很少变化的来源（如 `project_instructions`）尽管每轮都符合条件，实际注入很少；而内容经常变化的来源（如 `todo_block`）多数轮次都会注入。`cooldown_turns = 5`（TaskFlow 看板、boulder）表示*至多每 5 轮一次*，且仅在内容也确有变化时；未变化的看板即便本可注入，也会静默 5 轮。节流状态只为**真正注入过**的来源推进，被抑制的来源不会"白白消耗"冷却。`current_time` 是例外：内容带秒且关闭 `only_if_changed`，所以每轮都会出现。

**压缩过滤。** 每个 source 声明 `compress_exclude`（默认 `True`）；`DynamicReminderMiddleware` 把它写入消息 metadata，`_filter_summary_messages` 在构建摘要 prompt 前丢弃这些 reminder。live view 每轮都会重新派生，压缩前的快照就是噪音；`continuity_context` 是唯一设置 `compress_exclude=False` 的 source，序列化为 `[Continuity]`。标签出现前注入的旧 reminder 没有该键，继续走旧行为。真实用户轮次由 `pub.func.message.classify.is_user_message`（`metadata.origin == "user"`，fail-closed）识别，因此注入的 HumanMessage 在摘要里永远不会被标成 `[User]`。

**跨轮持久化。** 非 transient 的提醒是转录中的 `HumanMessage`；中间件每轮做 whole-list rewrite 时通过 `messages[:-1]` 原样重发，所以它会留在上下文窗口里直到压缩把它丢弃（压缩后 `reset_session` 清除变化检测 hash，下一轮从 live state 重新注入）。**Transient 来源**（`transient=True`）是例外：在它未被重新解析的轮次，rewrite 会从 `messages[:-1]` 中剥掉旧提醒，因此 `project_instructions` 只在内容变化的那一轮（以及压缩后的第一轮）出现；`current_time` 每轮重新解析，剥离上一轮的行可确保上下文中只有一行当前时钟，而不是每轮一行。模型读一次即可，不会占据后续每一轮。

**注入触发链。** 一个来源要在某一轮被注入，必须按顺序通过以下每一道关卡（`core.py` 的 `resolve_injections` + `dynamic.py` 的中间件门）：

1. **目录已启用** —— `REMINDER_CATALOG["enabled"]` 为 `True`；关闭则什么都不注入。
2. **本轮首个模型调用** —— 中间件仅在最后一条消息是真实（非注入）`HumanMessage` 时触发；turn 内的工具循环调用和已带提醒块的 resumed turn 被跳过（`_already_injected_this_turn`）。
3. **冷却已过** —— 若 `cooldown_turns > 0` 且来源曾被注入过，距上次注入须至少经过 N 轮；从未注入过的来源直接通过（不从 turn 0 开始冷却）。
4. **builder 返回内容** —— builder 执行；返回 `None`、空白或抛异常则跳过该来源（fail-open——一个坏来源不会拖垮整层）。
5. **内容已变化** —— 若 `only_if_changed=True`，计算内容的 SHA-256 hash 与上次注入的 hash 比对；内容相同则跳过。
6. **总预算** —— 存活候选按优先级降序排列；优先级最低者整条丢弃，直到剩余集合放得下 `REMINDER_CATALOG["total_budget_chars"]`（30 000）。被丢弃的来源**不算已展示**，下一轮仍符合条件。

**优先级即预算顺序。** `resolve_injections` 按优先级降序排列；在 `REMINDER_CATALOG["total_budget_chars"]`（30 000）上限下，优先级最低的来源会被整条丢弃（绝不截断半句），直到剩余集合放得下。投递时反转保留集合，使最高优先级的视图（计划模式，然后 todo 列表）落在块尾部、紧贴本轮用户消息——先实时状态，后请求。

**工具结果增强**是独立的逐调用队列：工具返回后把提示追加到 `ToolMessage`（JSON 工具写入 `advisory` 字段，纯文本追加后缀）。它与本回合级提醒层正交；见 [工具结果增强](../tool-result-enrichment/README.zh.md)。

**不属于本目录。** `TaskIntent` 转向、todo 续跑指令、workspace 通知各自保留独立注入路径，未在此注册；目录是叠加层，迁移分批进行。
**DelegationPressureGate**（`agent/middlewares/delegation_pressure/core.py`）把自己的 warn / escalated 指令以 `<system-reminder source="delegation_pressure">` 形式注入，并使用标准提醒 metadata（`provenance="reminder_catalog"`），但走它自己的 `abefore_model` 路径，而不是目录的 `resolve_injections`。目录只在每轮首个模型调用（最后一条是真实用户消息时）解析；delegation pressure 在回合中段（7 / 14 次工具调用后、后续模型调用）触发，目录的 first-call-only 门会挡住它。它概念上是最高优先级：触发时目录是 no-op，指令被追加到消息列表尾部、最贴近生成点。工具始终可用；它只警告，不阻断。
