# 🛡️ 产品侧的 B 类评测实战经验

> [English](README.md) · 中文 · [한국어](README.ko.md) · [日本語](README.ja.md)

> 四项从评测框架观察（external-bench / DeepSWE）与产品实测失败模式提炼的 fail-open 加固实践。与 A 类不同，这些是可观测性 / 防护栏：都不改变任何既有门禁的决策路径，失败时都降级为 no-op。权威来源：每节列出的文件。

## 1. PathGuard 拒绝计数（`agent/middlewares/path_guard/core.py`、`server/service/turn_runner.py`）

被拦截的工具调用会返回普通错误 `ToolMessage`，因此 agent 在 PathGuard 拒绝上循环时会产生数百次调用而用户看不到原因。PathGuard 现在在同步与异步拒绝分支都记录**每会话拒绝计数**（`record_denial` → `_PATH_DENIALS`）：次数 + 去重后的被拒路径。回合结束时 `on_turn_finished` 抽取计数（一次性）并推送一个瞬时 `system.notice` 卡片，标题为「PathGuard 拒绝了写/读操作」，含次数与最多五条路径。该通知复用既有会话内通知条，因此没有新增 socket 帧或 toast 处理器。会话销毁时调用 `clear_denials`，不留残留。决策路径不变：计数仅用于可观测性，空 session id 不计数，无内容时抽取返回 `None`。

## 2. 有 step 在飞时的 wait-only 指令（`agent/middlewares/todo_continuation/core.py`）

计划续跑门禁过去在有 TaskFlow step 子进程仍在运行时仍说「继续处理下一个待办任务」——agent 可能在四个子进程还在飞时宣布完成。当 `_has_in_flight_steps(flows)` 为真（任一 step 为 `dispatched` 或 `ready`）时，门禁现在选择 `[SYSTEM DIRECTIVE: STEPS IN FLIGHT]`：调用 `taskflow_step(action='wait_all')`，再 `resume` 已 settle 的 step，用 `taskflow_flow(action='finish')` 关闭流程，并且**在每一个子进程 settle 之前不得宣布完成或开启新工作**。没有 step 在飞时仍用普通续跑指令；blocked-only 仍适用于全阻塞形态（在飞与阻塞互斥）。状态块与 board footer 保留——指令只是收窄决策，不隐藏计划的其余部分。

## 3. 压缩时保留 reminder 快照（`agent/middlewares/reminder_catalog/dynamic.py`、`agent/middlewares/summarization/compression.py`）

回合中途压缩会把整个 cutoff 窗口——包括其中每个 `<system-reminder>`——替换成摘要对，导致模型在剩余回合没有 todo/TaskFlow 锚点。压缩流水线现在在截断之后（recovery context 改写之前）调用 `capture_reminder_snapshot`，把结果插入摘要对与保留尾部之间：`[summary, snapshot, preserved…]`。快照是每个非 transient source 在**压缩时刻**最后一条 reminder 的版本——不是 live state——因此模型看到一条时间线：摘要 → 压缩时的状态 → 更新的 preserved reminder。transient source（current_time、lsp_diagnostics、project_instructions）从不捕获。对每个 source，若快照内容与 preserved 中**最旧**的 reminder 逐字节相同则丢弃（纯冗余）；比较最旧而非最新，避免状态变了又变回来时误删快照。快照采用懒加载导入，仅在确实构造了摘要对时运行，任何错误都 fail-open 为「无快照」。动态 reminder 中间件、其守卫与下一回合的注入都不受影响。

## 4. 并行 tool 批次的聚合大小保护（`agent/safe_tool_node.py`、`pub/func/message/eviction.py`、`config/features/agent_side/tool_parallel.py`）

`ContextEviction` 在工具节点内**逐个**驱逐，因此 N 个并行结果各自刚好卡在 20K 字符阈值下时全部保留，合计却撑爆上下文窗口——没有任何 per-call 钩子看得到合计。`SafeToolNode` 现在在唯一持有整批数据的位置应用 `_enforce_batch_size_limit`：`gather` 之后、`_combine_tool_outputs` 之前，覆盖 enforce/non-enforce × sync/async 四条路径。合计超过 `TOOL_PARALLEL["batch_result_threshold_chars"]`（默认 100K，0 表示禁用）时，从最大的结果开始用既有 `evict_tool_result`（磁盘 offload + head/tail 预览）驱逐，失败回退 `hard_clip_tool_result`（内存截断），直到批次达标或无法再缩小。两个辅助函数在该路径接受 `force=True` 关键字，使单个 19K 结果即使低于 20K per-call 阈值也能被驱逐；默认调用方逐字节不变。已驱逐的结果与零大小项跳过；顺序与数量不变；任何失败都保留原结果。磁盘预览有约 4K 字符的下限，因此超大批次会被缩小数倍，而不是为了达标牺牲全部信号。异步路径通过 `asyncio.to_thread` 运行保护，绝不阻塞事件循环。既有 `read_file` 恢复提示修正为 `offset=1`（该工具是 1-indexed）。

## 安全不变量

- 四项都是**追加式**：没有不兼容地改变任何既有提示词契约、中间件顺序、工具 schema、状态 key 或回合生命周期钩子。
- 所有读取 / 保护路径都 **fail-open**：空 session id、空计数、store 故障、捕获错误或 offload 失败都会降级为「无信号 / 保留原结果」，绝不硬失败。
- 阈值统一放在 `TOOL_PARALLEL`；wait-only 规则复用既有 `_has_in_flight_steps` 判定；快照复用 reminder 目录的 transient-source 注册表。热路径里没有魔法数字。
