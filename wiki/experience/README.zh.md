# 经验体系架构

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

本文梳理经验体系：Agent 在运行过程中**何时**抽取经验、**以何种机制**抽取、经验**写到哪里**，以及产出的技能库如何被维护。生命周期中共接入四条抽取路径：压缩时 memory review（每次压缩；只收到即将被丢弃的消息，并以"缓存命中"方式跑在本轮自己的模型与前缀上）、计划在轮次边界完成时的 **技能评审**、压缩后 todo fork，以及主会话删除时的 session-end salvage（对捕获的压缩切口运行后台两波 memory review）。产出分别落入四个存储——MEMORY.md / USER.md / FACTS.md、`skills/auto/`、`registry.db`——外加原始 `handoff-tree.json` 审计文件；下文 **Curator** 一节记录维护 `skills/auto/`（技能评审 的写入目标）的后台流程。

> **存储更新（2026-11-04）：** 下文的 MEMORY.md / USER.md / FACTS.md 三个记忆产出，现已改为 MesMemory `memory_entries` 表中的行，每个 target（`memory` / `user` / `facts`）各占若干行。遗留 Markdown 文件由 `memory_migration.py` 在启动时导入一次并改名为 `*.md.bak`。加载入口是 `MemoryStore.load_from_store()`；每行携带 `origin_session`（来源）与 `usage_count` / `last_used`（用量反馈）。

> **模块拆分（2026-11-13）：** 后台复盘任务现在位于 `agent/middlewares/summarization/` 下的专用文件：`memory_review.py`、`session_end_salvage.py`、`handoff_tree.py`、`skill_review_runner.py`、`ptc_compile.py`、`compression_todo.py`，共享的 `background_scaffold.py` / `_common.py`；`skill_review.py` 仍是质量门。`background_reviews.py` 变成 re-export facade，其模块代理会把属性写入转发到实现模块，因此历史符号与 monkeypatch 接缝保持不变。

> 下文每条断言均已对照源码核实。符号名、配置键、默认值与路径均真实存在于 `agent/middlewares/`、`agent/tools/`、`config/features/` 的代码中。

## 设计原则

1. **所有抽取都扩展既有存储。** 产出分别落到 MEMORY.md / USER.md / FACTS.md、`skills/auto/`、或 `registry.db`；原始 handoff tree 只作审计，不作为面向模型的知识存储。没有任何抽取路径另建平行存储。
2. **全部 fail-open。** 每个触发点都记录并吞掉自身失败。todo 存储损坏、plan 文件不可读、LLM 调用失败，都不会阻塞或打断主对话回合。
3. **回合路径零阻塞。** 压缩后 todo fork 与压缩时后台评审 都是 fire-and-forget 后台任务；memory review 与 技能评审 作为独立子 agent 在 BACKGROUND_REVIEW 车道上运行，绝不阻塞模型调用。
4. **每条抽取路径都需要工具。** memory review、技能评审 与 todo fork 都是 `create_agent` fork，因为各自都要通过工具写入（`memory`、`skill_manage`、`todowrite`）；不再存在独立的纯抽取调用。
5. **计划完成只维护 skill。** plan 完成后的通道不写 knowledge、不写 FACTS；持久产出是类级 skill 变更，或 judge 认可的 no-op。

## 触发 × 机制 × 落点

| 触发 | 机制（是否 fork agent / 调用形态） | 写入目标 |
|---|---|---|
| 每次压缩 | 记忆复盘（`_execute_memory_review`）：只收**被丢弃**的那段，**原文**发在"本轮刚用过的"模型配置上（`_build_session_model`），并复刻会话的系统提示词与工具面，使请求命中前缀缓存；`BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"` 则改为折叠（最近 24 条原文，更早的每条截断成一行） | 经 `memory` 工具写入 MEMORY.md / USER.md / FACTS.md |
| 计划完成——轮次边界上全部 todo 为 `completed` / `cancelled`（**不在压缩时**） | 技能评审（`_run_skill_review`）：完成钩子（`PlanContinuationEnforcer` → `maybe_trigger_skill_review`）用**辅助模型** fork 专用 agent，使用 `_SKILL_REVIEW_PROMPT`，且**不带任何对话历史** | `skills/auto/`（类级技能 + `<module>-notes`）；允许 judge 认可的 no-op；原始 `handoff-tree.json` 先写盘审计 |
| 压缩后（cut 确实丢弃了消息） | todo fork（`update_todos_from_compaction`）：fire-and-forget 后台评审 agent，使用 `_COMPRESSION_TODO_PROMPT` | 经绑定主会话的 `todowrite` 垫片写入 `registry.db` |
| 删除主会话（purge 之前） | session-end salvage（`salvage_session_memory`）：快照 checkpoint transcript，捕获模型/系统提示词/工具面，只读计算压缩切割线，然后运行两次独立 memory review——第一波处理本会被压缩的前缀，第二波在重载 memory 并重建提示词后处理保留的尾部 | 经 `memory` 工具写入 MEMORY.md / USER.md / FACTS.md |

## 触发详情

### 1. 压缩时 memory review

`schedule_memory_review`（`agent/middlewares/summarization/background_reviews.py`，由 Summarization 中间件在每次真正丢弃消息的 compact 时调用）每次压缩都以 fire-and-forget 任务派发 `_execute_memory_review(session_id, system_prompt, messages_to_summarize)`，在 `background_review_memory_lock`（`state_register_mem`）保护下运行。交给它的是**被丢弃**的那段——要丢的事实正在那里，而且只占整段对话的一小部分——默认 `cache_warm` 模式下这段**原文**直接发给模型（`BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"` 才先用 `digest_history` 折叠）。任一评审锁被持有时，压缩完全跳过派发（不排队）。

`_execute_memory_review`（`agent/middlewares/summarization/background_reviews.py`）通过 `_create_memory_review_agent` 构建复盘 agent，并把渲染后的 `_MEMORY_REVIEW_PROMPT` 作为 `HumanMessage` 追加到被丢弃段后调用。**构造上就是缓存命中：** 压缩切点是前缀切点，被丢弃段恰是请求中紧跟系统提示词的那部分——用同一模型配置（`_build_session_model`：会话的 provider/model/思考/temperature；因图在每轮重建后会关闭客户端，故按配置**新建实例**）、同一系统提示词与同一工具面把它发回去，就复现了主模型自己的前缀，提供商随后从前缀缓存作答；新增输入只有那条复盘指令，而复盘读到的仍是**真实原文**。两道防爆不变：只传被丢弃段而非整段对话（绝不传保留段），且绝不阻塞本轮（fire-and-forget，跑在 BACKGROUND_REVIEW 车道上）——这次复盘恰好在上下文最满时运行。该提示要求 agent 用 `memory` 工具把持久的用户特征（persona、偏好、个人细节）与行为期望写入 `user` 目标，把系统级 / 跨项目普适的坑写入 `facts` 目标（项目级事实写入 MEMORY.md 或 AGENTS.md）。提示渲染时会带上当前 FACTS.md 内容与字符上限（`_render_prompt_facts`），并要求 agent 像编辑一样维护该文件——合并同类、替换过时条目、宁缺毋滥。若无内容可存，则回复 "Nothing to save." 并停止。

- 复盘 agent 是会话自身模型（`_build_session_model`，用于前缀缓存复用）上独立的 `create_agent`，中间件为 `[_BackgroundReviewLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`，无 checkpointer。
- `_BackgroundReviewLimitTool`（未指定 `allowed_metadata_key`）只放行 metadata 带 `background_review: True` 的工具。`memory` 带该标记（另有交互式 `question` 与两个计划模式工具），因此记忆复盘能写 memory，但无法调用任意主工具；已退役的 `knowledge` 工具不再出现在工具面中。技能三件套（`skill_list` / `skill_view` / `skill_manage`）**刻意不带**——技能抽取归计划完成通道，它不靠该标记，而是显式绑定自己的工具面。
- fork 的消息只记录日志。`res["messages"]` 中的任何内容都不会进入主图。

### 2. 技能评审（todo 完成，在轮次边界）

`_detect_todo_all_complete(session_id)` 在计划可能完成的每个回合评估，每个完成周期只触发一次：todos 存在且全部为 `completed` / `cancelled`；`skill_review_fired` 尚未设置。它设置 fired 标志，列表不完整时重置；读取全部 fail-open。完成钩子现在每回合检查，短会话也能抽取。

标志本身看不到 completed todo 的原地修改，因此 trigger 还比较 `_plan_snapshot_hash`（id + content + status + plan_ref）与上次**成功** pass 后存入 `skill_review_snapshot_hash` 的值——内容变化才重新抽取，计划未变则跳过，失败 pass 在 cooldown 后重试。

`skill_review_enabled` 与 `skill_review_enabled` 都开启时，hook 以 fire-and-forget 方式在 BACKGROUND_REVIEW 车道运行 `_run_skill_review`：

1. `_build_plan_context` 读取 plan 文件、todos、子代理 runs（result_text 截断 24 KB），以及有界 `feedback`：最近消息中的 `skill_view` 调用 + `.usage.json` 最近条目（`loaded_skills`），以及最近回合的用户纠正（`user_corrections`）。internal carrier、system reminder、workspace notice 都被排除。没有 todo 返回 `{}`，pass 跳过。
2. LLM 之前先把原始合并 handoff tree 原子写入 `handoff-tree.json` 审计；它不是面向模型的知识读取入口。
3. `_SKILL_REVIEW_PROMPT` 用压缩上下文渲染，交给专用 agent（辅助模型，temperature=0，**不带对话**）。工具面显式绑定 `skill_list` / `skill_view` / `skill_manage`；`knowledge` 和 `memory` 不在其中，因此不能写知识或 FACTS。
4. Prompt 明确映射证据：`success_steps` → 步骤/验证，`failed_branches` → pitfalls / `when_not_to_use`，`hard_constraints` → guardrails，`locked_decisions` → rationale，`user_corrections` → body / `when_to_use`。优先 patch 本次加载的 skill，其次已有类级 umbrella，再次支持文件，最后才新建类级 skill。只有带具体理由才允许 no-op。

实现细节：`summarization/background_reviews.py` 的 `_run_skill_review` 使用 `_create_skill_review_agent`，在 `skill_review_lock` 下运行；`state_register_db` 保存指纹，`outcome` 来自 subagent runs，失败标志为 `False`。

技能评审以 snapshot/judge 事务运行：

- **snapshot**：`skill_review.snapshot_auto_skills()` 在首次尝试前捕获 `skills/auto`（技能、支持文件、usage sidecar）。
- **write**：评审创建/修补 skill；`set_current_write_origin("background_review")` 使新 skill 标记为 agent-created，供 curator 管理。
- **judge**：`skill_review.judge_skill_update()` 用辅助模型返回 `verdict`、`grounded_in_run`、`reusable`、`trigger_quality`、`quality`、`no_op_justified`、`feedback`。只有 `quality >= BACKGROUND_REVIEW_CONFIG["skill_review_quality_threshold"]`、`trigger_quality >= 6`、grounded、reusable 才通过；no-op 需要 `no_op_justified`。
- **retry/rollback**：失败尝试恢复 pre-review snapshot，带 judge feedback 重试（默认最多 2 次）。耗尽后再次恢复，记录 `skill_review_failed_hash` + 时间戳，不推进成功指纹；后续回合在 `skill_review_failure_cooldown_s` 后可重试，计划变化立即重试。
- **可选 PTC 编译（通过后）**：当 `PTC["ptc_prefill_enabled"]` 开启时，同一后台任务会为每个成功的 TaskFlow step 向更新后的 skill 编译 `scripts/ptc__<plan_key>__<step_id>.py`。编译是 best-effort，绝不重跑 judge，也不回滚已通过的 skill。

此路径不再写 knowledge（`task-*.json` / `wave-*.json` / `plan-summary.json`）或 FACTS。旧 knowledge 文件留在磁盘但属于 legacy；面向模型的 prompt block 和 `knowledge` 工具已退役。

### 3. 压缩后 todo fork

在同一压缩点，`_schedule_compression_todo_update`（`summarization/core.py` -> `background_reviews.schedule_compression_todo_update`）把 `update_todos_from_compaction` 作为 fire-and-forget `asyncio.create_task` 调度。调度器需**全部**满足：

- `compression_todo_update_enabled`（`SUMMARIZATION`，默认 `True`）；
- cut 确实丢弃了消息；
- 该会话未持有 `compression_todo_update_lock`；
- 会话有非空 todo 列表；
- 存在运行中的事件循环（同步压缩路径会跳过，并打 debug 日志）。

`update_todos_from_compaction` 运行一个后台评审 agent，系统提示为 `_COMPRESSION_TODO_PROMPT`，工具恰好一个：`_build_main_session_todowrite(session_id)`。该 fork 依据被丢弃片段核对当前 todo 列表：把实际完成的项标为 `completed`，把放弃或被取代的项标为 `cancelled`，把有证据的新工作加为 `pending`，并在一次 `todowrite` 调用中写回**完整**列表（全量替换，而非增量）。若无变化，则原样写回。

fork 的结果消息只记录日志。任何内容都不会进入主图或其 checkpointer，派生会话的中间件状态在 `finally` 中清理。

### 4. 会话删除（仅主会话）

`purge_session` 销毁主会话前，`salvage_session_memory` 对压缩后的 checkpoint transcript、模型、系统提示词和工具面做快照，只读计算正常压缩切割线，然后调度一个独立的后台两波复盘。删除请求不等待模型：同步阶段只有快照，预算 5 秒。侧边会话和子代理不进入此路径。

- **第一波**处理 `messages[:cutoff]`——本会被压缩丢弃的前缀——使用捕获的模型、系统提示词和工具面，保持与压缩时复盘相同的 cache-warm 形态；不生成摘要，不触碰压缩状态。
- **第二波**处理 `messages[cutoff:]`——保留的尾部。第一波写入 memory 后，任务重新加载 `MEMORY.md`/`USER.md`/`FACTS.md`，非持久化重建系统提示词（会话已删除），并用仅 `memory` 工具面复盘尾部。重建失败则回退到捕获的提示词。
- 两波都在 BACKGROUND_REVIEW 车道运行，各自使用派生的会话键（`<session_id>::session-end-salvage:1|2`），无 checkpointer；键在 `finally` 清理。`memory` 工具的 `background_review_memory` metadata 是执行门禁，因此第一波可以为了 cache-warm 复刻会话工具面，但删除后无法调用 `knowledge` 或 plan 工具。
- 快照、提示词重建、超时、模型异常等所有失败都记录并吞掉。进程退出可能放弃未完成的后台复盘，这是立即删除的既定取舍。配置：`BACKGROUND_REVIEW_CONFIG["session_end_review_enabled"]` 与 `BACKGROUND_REVIEW_CONFIG["session_end_review_timeout_s"]`（每波，默认 120 秒）。

## Curator（技能策展）

**frontmatter 契约（索引就是触发器）。** `<available_skills>` 只渲染技能的 frontmatter —— 正文要加载后才读 —— 所以这些字段就是路由信号：`description`（能力概述）、`when_to_use`（**必填**：这些路径创建或修补的每个技能都要写，写明应该加载它的具体场景／用户意图）与 `when_not_to_use`（可选：排除条件与替代方案）。加载器用 CJK 感知的 token 预算（60 / 50 / 50 token）渲染这两个附加标签并做 XML 转义；前端技能 tooltip 展示同样三个字段。curator 还加了更窄的第二层：伞状技能的 frontmatter 覆盖整个技能，而每个 `##` 子能力段落以自己的一行 `**When to use:**` 开头（确有排除时才加 `**When NOT to use:**`），模型 `skill_view` 后即可选对段落。它的确定性回退写出同样的形状，从合并理由里启发式提取这些条件。

Curator（`context_engine/curator/`）是维护 `skills/auto/` 技能库生命周期的后台流程——正是上文抽取路径 2 的写入目标。它只读写技能；MEMORY.md / USER.md / FACTS.md 与 `registry.db` 都不在其范围内。

**是什么。** 它是空闲触发的编排器，而非定时 cron。服务入口通过 `context_engine.curator.init()` 启动守护线程（`curator-timer`）（`server/__main__.py:140`；HTTP-only 模式下跳过，`server/__main__.py:66-73`）。线程每 3600 秒唤醒一次并调用 `maybe_run_curator(idle_for_seconds=...)`（`context_engine/curator/__init__.py:122-140`）。导入该包无副作用——只有 `init()` 会启动线程（`context_engine/curator/__init__.py:151-165`）。

**何时运行。** 只有两道门都通过才会执行：

- `should_run_now()`——已启用、未暂停，且 `last_run_at` 已超过生效间隔（`context_engine/curator/transitions.py:21-39`）。生效间隔取自 `curator.interval_hours`，默认 168 小时 / 7 天（`config/sherry_settings.py:43`），客户端可通过 `.curator_state` 中的 `auto_interval_days` 覆盖为 1–5 天（`context_engine/curator/config.py:97-107`）；
- Agent 空闲时长达到 `curator.min_idle_hours`，默认 2 小时（`context_engine/curator/orchestrator.py:108-115`）。每次用户回合都会重置该空闲计时器（`server/service/messages.py:190`）。

UI 可通过 `POST /curator/run` 强制触发一次运行，它在工作线程中调用 `run_curator_review()`（`server/trigger/http/curator.py:129-160`）。归档入口就在旁边：`POST /curator/restore`（body `{"name": "<skill>"}` → `{success, message}`；未知名称、bundled/hub 遮蔽或目标已存在返回 HTTP 400）和 `GET /curator/archived`（`{success, archived, count}`），两者都由 curator 层 `restore_skill()` / `list_archived()` 支撑（`server/trigger/http/curator.py:163-204`）。

**生命周期规则。** `apply_automatic_transitions()`（`context_engine/curator/transitions.py:41-91`）遍历每个 `skills/auto/**/SKILL.md`（`context_engine/curator/usage.py:161-180`），对每个技能：跳过 pinned 技能；无活动达到 `stale_after_days`（默认 30 天）标记为 `stale`；超过 `archive_after_days`（默认 90 天）则**归档**到 `skills/.archive/` 并标记为 `archived`；重新出现活动、或处于 stale 窗口内但从未使用的技能会被重新激活。**三种移除全部可恢复**：90 天流转、合并源技能（归档时写入 `ABSORBED_INTO` 标记注明 umbrella）、被剪枝的技能，统一进入 `skills/.archive/`，`curator restore <name>` 均可拉回。恢复会在同一步把 curator 侧记录翻回 `active`（agent 侧转发器同时同步 agent 遥测记录），恢复后的技能立即重新进入 `apply_automatic_transitions`，而不会四个分支全不命中；同一秒的归档冲突得到互不嵌套的平级目录（`<skill>-<timestamp>`）。只有显式删除——`skill_manage(action="delete")` 或客户端的删除端点——才会不可逆地移除技能。归档目录不会自动清空——可用 `curator restore` 恢复或手动清理。默认值位于 `config/sherry_settings.py:42-48`。

**LLM 合并。** 当 `curator.consolidate` 开启（默认开）时，`run_curator_review()` 渲染非 pinned 技能候选列表（`context_engine/curator/review.py:69`），让主 LLM 以 temperature 0.3 把重叠的窄技能合并为类级 umbrella 技能（`CURATOR_REVIEW_PROMPT`，`context_engine/curator/review.py:16`）。新 umbrella 及其支持文件生成后写入 `skills/auto/`（`_generate_umbrella_skill`，`context_engine/curator/umbrella.py:86`；`_apply_consolidation`，`context_engine/curator/orchestrator.py:163`）。合并是该流程唯一的 LLM 步骤；失败会被捕获，运行继续。

**与四条抽取路径的关系。** 路径 2（压缩时 skill review）是生产者：它经 `skill_manage` 创建或修补 `skills/auto/`。Curator 是同一产出的下游维护者——对 skill review 创建的技能做流转、合并与修剪。另外三条路径从不触碰 `skills/auto/`（分别写 MEMORY.md / USER.md / FACTS.md、`registry.db`，以及删除时的 memory 抢救），因此与 Curator 没有交集。

**状态与边界。** 运行状态存于 `skills/.curator_state`（`context_engine/curator/constants.py:3`，由 `context_engine/curator/state.py` 读写）；每次运行在 `logs/curator/{timestamp}/` 下写出 `run.json` + `REPORT.md`（`context_engine/curator/constants.py:4`；`context_engine/curator/report.py:196-205`）。范围仅限 `skills/auto/`——绝不触碰内置技能，pinned 技能跳过所有破坏性流转，整个流程在后台线程运行，不占对话回合路径。完整细节：[curator/README.md](../../context_engine/curator/README.zh.md)。

## 隔离与安全

| 防护 | 保护内容 |
|---|---|
| skill-review 工具面 | 只绑定 `skill_list` / `skill_view` / `skill_manage`；knowledge/memory 不存在。 |
| skill-review 回滚 | 每次尝试先快照 `skills/auto`；judge 拒绝时先恢复该快照再重试。 |
| 后台评审 metadata 白名单（`metadata["background_review"]`，`_BackgroundReviewLimitTool` 默认规则） | memory review 与 skill review 只能调用带 background_review 标记的工具。其它任何主工具都返回错误 `ToolMessage`，不执行。 |
| 压缩 metadata 白名单（`metadata["todo_update"] is True`，`_BackgroundReviewLimitTool(allowed_metadata_key="todo_update")`） | 压缩 fork 恰好只放行带该 metadata 标记的 `todowrite` 垫片。 |
| 派生会话键 `<id>::compression-todo` | 压缩 fork 的 `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 状态键不会与主会话冲突，因为 fork 运行期间主会话正处在 `awrap_model_call` 中。只有 `compression_todo_update_lock` 有意写在主会话上，作为跨路径防重入协调器。 |
| 派生会话键 `<id>::session-end-salvage:1|2` | 删除后的两波复盘运行在每波派生键下，引擎状态在 `finally` 中清理，不会复活已删除会话的 state；任务集合 `_SESSION_END_REVIEW_TASKS` 持有强引用。 |
| `background_review_memory` 执行门禁（`metadata["background_review_memory"] is True`，`_BackgroundReviewLimitTool(allowed_metadata_key="background_review_memory")`） | 第一波可以为 cache-warm 复刻会话工具面，但只有 `memory` 可执行；删除后 `knowledge` 与 plan 工具被拒绝。 |
| 绑定主会话的 `todowrite` 垫片 | fork 图运行在派生键下，因此由状态注入的真实 `todowrite` 会解析到错误会话。垫片逐字复用真实工具的 `args_schema` 与 `description`（零 schema 漂移），丢弃注入的 `session_id`，并绑定构建时捕获的主会话 id。 |
| 只读 fork、无 checkpointer | 每个后台评审 / 技能评审 fork 的结果消息都只记录并丢弃。fork 无 checkpointer，无法写主图状态。 |
| 每会话防重入锁 | `background_review_memory_lock`、`skill_review_lock`、`compression_todo_update_lock` 阻止同一抽取路径重叠运行。任一评审锁被持有时，压缩调度器完全跳过派发（不排队）。 |
| fail-open 边界 | 每条路径都用 `try/except` 包裹工作、记录日志并返回。任何抽取失败都不会传播进回合、压缩或其它抽取。 |
| 后台任务引用保留 | `_COMPRESSION_TODO_TASKS` 与 `_SESSION_END_REVIEW_TASKS` 集合持有强引用，防止 asyncio 回收在途任务。 |

## 存储与上限

| 存储 | 路径（常量） | 上限 |
|---|---|---|
| `memory` target | MesMemory `memory_entries` 行（`target='memory'`） | 2200 字符（`MEMORY_TOOL["memory_char_limit"]`，可在 `/memory/settings` 覆盖） |
| `user` target | MesMemory `memory_entries` 行（`target='user'`） | 1375 字符（`MEMORY_TOOL["user_char_limit"]`，可覆盖） |
| `facts` target | MesMemory `memory_entries` 行（`target='facts'`，唯一全局 target） | 1375 字符（`MEMORY_TOOL["facts_char_limit"]`，可覆盖）；`add` 超限时归档最不活跃条目 |
| legacy plan knowledge | `workspace/knowledge/plans/<plan_key>/` | 已退役：旧 `task-<n>.json` / `wave-<n>.json` / `plan-summary.json` 留在磁盘，但模型面向路径不再写入或读取 |
| handoff audit | `workspace/knowledge/plans/<plan_key>/handoff-tree.json` | 评审前写入的原始 handoff tree；不是 prompt block |
| 技能 | `config.path.AUTO_SKILLS_DIR`（`skills/auto/`） | `SKILL.md` 及 `references/`、`templates/`、`scripts/` 支持文件；PTC 为 `ptc__<plan_key>__<step_id>.py`，LDW 为 `ldw__<plan_key>.py` |
| todos | `src/data/registry.db` (`todos` table)（`store_sqlite._DB_PATH`） | 会话作用域列表，全量替换写入 |

## 配置开关

| 键 | 位置 | 默认值 | 作用 |
|---|---|---|---|
| `compression_todo_update_enabled` | `SUMMARIZATION`（`config/features/agent_side/summarization.py`） | `True` | 启用压缩后 todo fork |
| `skill_review_enabled` | `BACKGROUND_REVIEW_CONFIG`（`config/features/agent_side/background_review.py`） | `True` | 技能评审主开关（skill-only，不写 knowledge） |
| `skill_review_max_attempts` | `BACKGROUND_REVIEW_CONFIG` | `2` | 初次尝试 + 最多一次 judge feedback 重试 |
| `skill_review_quality_threshold` | `BACKGROUND_REVIEW_CONFIG` | `6` | 通过所需最低 `quality` |
| `skill_review_feedback_turns` / `_max_items` / `_chars` | `BACKGROUND_REVIEW_CONFIG` | `12` / `8` / `2000` | 有界用户纠正上下文 |
| `skill_review_loaded_skills_max` | `BACKGROUND_REVIEW_CONFIG` | `20` | loaded-skill 列表上限 |
| `skill_review_failure_cooldown_s` | `BACKGROUND_REVIEW_CONFIG` | `3600` | 同一失败 plan snapshot 的重试冷却 |
| `session_end_review_enabled` | `BACKGROUND_REVIEW_CONFIG` | `True` | 启用主会话删除时的抢救 |
| `session_end_review_timeout_s` | `BACKGROUND_REVIEW_CONFIG` | `120` | 删除抢救每波的模型调用预算 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 实际压缩后的主动压缩冷却 |
| `memory_char_limit` / `user_char_limit` / `facts_char_limit` | `MEMORY_TOOL`（`config/features/agent_side/memory_tool.py`） | `2200` / `1375` / `1375` | MEMORY.md / USER.md / FACTS.md 上限 |

## 验证

验证说明：Persistence assertions live in `tests/agent/middlewares/message_persistence/`；`skill_view` 记录加载；新增 `test_skill_review.py`、`test_skill_review_feedback.py`、`test_skill_review_gate.py`。

定向测试：

```bash
uv run pytest \
    tests/agent/middlewares/test_session_end_review.py \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_background_reviews.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/system_prompt/test_skill_review.py \
    tests/agent/middlewares/test_skill_review_module_notes.py \
    tests/agent/middlewares/test_skill_review_gate.py \
    tests/agent/middlewares/test_skill_review_feedback.py \
    tests/agent/middlewares/test_skill_review.py \
    tests/agent/tools/test_memory_store.py \
    tests/agent/tools/test_memory_store_facts.py -q
```

- `test_session_end_review.py`：删除快照、只读压缩切割、第一波 → 提示词重建 → 第二波顺序、cutoff/空/开关/重复/失败分支、派生状态清理，以及两波都写入 MEMORY.md/USER.md。
- `test_compression_todo_update.py`：触发门槛、fire-and-forget 调度、防重入锁、fail-open 释放、提示内容、`todo_update` metadata 门禁，以及完整 fork 隔离（派生键、主会话 `todowrite` 垫片、无 checkpointer / 消息泄漏）。
- `test_compression_background_reviews.py`：压缩时派发（memory review 从 compact 接缝触发；无切点压缩不派发）。`test_memory_review_digest.py` 钉住被丢弃段的接线、cache_warm 的模型重建（descriptor 字段、环境默认回退、会话工具面），以及 `memory_review_mode="digest"` 下的折叠。持久化断言位于 `tests/agent/middlewares/message_persistence/`。
- `test_compression_cooldown_persist.py`：冷却跨重启存活。
- `test_skill_review_trigger.py`：`_detect_todo_all_complete` 四个分支、`schedule_memory_review` 的每次压缩派发记忆回顾与锁语义，以及 `_build_plan_context`。
- `test_skill_review_module_notes.py`：`<module>-notes` carry-forward 指引、技能评审提示中的实时 FACTS.md 渲染，以及假 LLM 行为测试（create 落到 `skills/auto/<module>-notes/`；同名已存在则 patch，绝不重复创建）。
- `test_skill_review_gate.py`：snapshot/restore 回滚、变更路径检测、judge 的 pass/retry/no-op/unavailable 判定。
- `test_skill_review_feedback.py`：有界用户纠正、内部载体过滤、`skill_view` 加载技能抽取与 usage fallback。
- `test_skill_review.py`：judge 认可的 no-op、带 feedback 的重试、快照回滚、耗尽后的失败标记与失败冷却。
- Persistence assertions live in `tests/agent/middlewares/message_persistence/`.
- `test_memory_store.py`：MEMORY.md / USER.md 存储语义。
- `test_memory_store_facts.py`：`facts` target——add/replace/remove、超限滚动淘汰最旧、原子写、加载时创建文件、实时内容渲染与提示快照注入（空文件不产生空块）。

AI 评判评估：`evals/skill_review/suite.py` 构造一次已完成 plan 的运行（plan 文件、已完成 todos、合成的子 agent 运行记录），调用真实的 `_run_skill_review`，再让辅助 LLM judge 判断产出的技能是否真正扎根于本次运行、可复用且非泛泛而谈。所有写入都重定向进沙箱，真实 `skills/auto/` 与 `workspace/` 绝不被触碰。

```bash
uv run python evals/evals.py skill_review
```

该套件检查 `knowledge_retired`、`skills_created`、`ai_judge_skill_review` 与 `no_repo_pollution`。

## 已知边界

- **压缩 fork 有意不给 `todoread`。** 只放行带 `todo_update` 标记的 `todowrite` 垫片；fork 在提示中已收到当前列表，`todoread` 有意不打标记（`agent/tools/todolist/tools/__init__.py`）。
- **FACTS.md 是单文件记忆。** 它与 MEMORY.md / USER.md 同机制（单文件、`§` 分隔条目、原子改名写入、每文件字符上限），并注入每次系统提示；它不是分层/分类存储，超出上限时最旧条目滚动淘汰。
- **冷却只抑制主动压缩。** `compaction_cooldown_rounds`（3）在实际压缩后阻止 T1 / T2 / T3 压缩；T4 / T5 提供方错误恢复在构造上绕过冷却、每回合尝试上限等反抖动门槛。

## 相关文档

- [会话级内存架构](../session_memory/README.md)：承载这些存储的会话级内存架构。
- [摘要压缩](../summarization/README.md)：压缩触发，以及门控后台评审与 todo fork 的冷却。
- [长程任务](../long-running-tasks/README.md)：TaskFlow，以及被 skill review 读取的 todo 规划层。
- [中间件 README](../../agent/middlewares/README.md)：`@dynamic_prompt` 系统提示词注入与 Summarization 参考。
- [Curator README](../../context_engine/curator/README.zh.md)：上文所述的后台技能维护编排器。
