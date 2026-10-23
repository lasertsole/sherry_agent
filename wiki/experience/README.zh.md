# 经验体系架构

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

本文梳理经验体系：Agent 在运行过程中**何时**抽取经验、**以何种机制**抽取、经验**写到哪里**，以及产出的技能库如何被维护。生命周期中共接入四条抽取路径：压缩时 memory review（每次压缩；只收到即将被丢弃的消息，并以"缓存命中"方式跑在本轮自己的模型与前缀上）、计划在轮次边界完成时的 plan extraction、压缩前 memory flush、压缩后 todo fork。产出分别落入五个存储——MEMORY.md / USER.md / FACTS.md、plan 知识目录、`skills/auto/`、`todos.db`——下文 **Curator** 一节记录维护 `skills/auto/`（plan extraction 的写入目标）的后台流程。

> 下文每条断言均已对照源码核实。符号名、配置键、默认值与路径均真实存在于 `agent/middlewares/`、`agent/tools/`、`config/features/` 的代码中。

## 设计原则

1. **所有抽取都扩展既有存储。** 产出分别落到 MEMORY.md / USER.md / FACTS.md、plan 知识目录、`skills/auto/`、或 `todos.db`。没有任何抽取路径另建平行存储。
2. **全部 fail-open。** 每个触发点都记录并吞掉自身失败。todo 存储损坏、plan 文件不可读、LLM 调用失败，都不会阻塞或打断主对话回合。
3. **回合路径零阻塞。** 压缩后 todo fork 与压缩时 nudge 都是 fire-and-forget 后台任务；memory review 与 plan extraction 作为独立子 agent 在 NUDGE 车道上运行，绝不阻塞模型调用。
4. **机制按需匹配。** 需要工具调用的工作才用完整 `create_agent` fork（memory nudge、plan extraction、todo fork）。纯抽取（memory flush）只用一次辅助 LLM 调用。

## 触发 × 机制 × 落点

| 触发 | 机制（是否 fork agent / 调用形态） | 写入目标 |
|---|---|---|
| 每次压缩 | 记忆复盘（`_nudge_memory`）：只收**被丢弃**的那段，**原文**发在"本轮刚用过的"模型配置上（`_build_session_model`），并复刻会话的系统提示词与工具面，使请求命中前缀缓存；`NUDGE["memory_review_mode"] = "digest"` 则改为折叠（最近 24 条原文，更早的每条截断成一行） | 经 `memory` 工具写入 MEMORY.md / USER.md / FACTS.md |
| 计划完成——轮次边界上全部 todo 为 `completed` / `cancelled`（**不在压缩时**） | plan extraction（`_run_plan_extraction`）：完成钩子（`PlanContinuationEnforcer` → `maybe_trigger_plan_extraction`）用**辅助模型** fork 一个专用 agent，使用 `_PLAN_EXTRACTION_PROMPT`，且**不带任何对话历史** | ① 知识 JSON ② `skills/auto/`（类级技能 + `<module>-notes`） ③ FACTS.md |
| 压缩前（cut 确实丢弃了消息） | memory flush（`run_memory_flush[_sync]`）：一次廉价 LLM 调用，非 agent | 经 `MemoryStore.append_entries` 写入 MEMORY.md 与 USER.md |
| 压缩后（cut 确实丢弃了消息） | todo fork（`update_todos_from_compaction`）：fire-and-forget nudge agent，使用 `_COMPRESSION_TODO_PROMPT` | 经绑定主会话的 `todowrite` 垫片写入 `todos.db` |

## 触发详情

### 1. 压缩时 memory review

`schedule_memory_review`（`agent/middlewares/summarization/nudges.py`，由 Summarization 中间件在每次真正丢弃消息的 compact 时调用）每次压缩都以 fire-and-forget 任务派发 `_nudge_memory(session_id, system_prompt, messages_to_summarize)`，在 `nudge_review_memory_lock`（`state_register_mem`）保护下运行。交给它的是**被丢弃**的那段——要丢的事实正在那里，而且只占整段对话的一小部分——默认 `cache_warm` 模式下这段**原文**直接发给模型（`NUDGE["memory_review_mode"] = "digest"` 才先用 `digest_history` 折叠）。任一 nudge 锁被持有时，压缩完全跳过派发（不排队）。

`_nudge_memory`（`agent/middlewares/summarization/nudges.py`）通过 `_create_memory_review_agent` 构建复盘 agent，并把渲染后的 `_MEMORY_REVIEW_PROMPT` 作为 `HumanMessage` 追加到被丢弃段后调用。**构造上就是缓存命中：** 压缩切点是前缀切点，被丢弃段恰是请求中紧跟系统提示词的那部分——用同一模型配置（`_build_session_model`：会话的 provider/model/思考/temperature；因图在每轮重建后会关闭客户端，故按配置**新建实例**）、同一系统提示词与同一工具面把它发回去，就复现了主模型自己的前缀，提供商随后从前缀缓存作答；新增输入只有那条复盘指令，而复盘读到的仍是**真实原文**。两道防爆不变：只传被丢弃段而非整段对话（绝不传保留段），且绝不阻塞本轮（fire-and-forget，跑在 NUDGE 车道上）——这次复盘恰好在上下文最满时运行。该提示要求 agent 用 `memory` 工具把持久的用户特征（persona、偏好、个人细节）与行为期望写入 `user` 目标，把系统级 / 跨项目普适的坑写入 `facts` 目标（项目级事实写入 MEMORY.md 或 AGENTS.md）。提示渲染时会带上当前 FACTS.md 内容与字符上限（`_render_prompt_facts`），并要求 agent 像编辑一样维护该文件——合并同类、替换过时条目、宁缺毋滥。若无内容可存，则回复 "Nothing to save." 并停止。

- 复盘 agent 是**辅助 LLM** 上独立的 `create_agent`，中间件为 `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`，无 checkpointer。
- `_NudgeLimitTool`（未指定 `allowed_metadata_key`）只放行 metadata 带 `nudge: True` 的工具。`memory` 与 `knowledge` 带该标记（另有交互式 `question` 与两个计划模式工具），因此记忆复盘能写 memory，但无法调用任意主工具。技能三件套（`skill_list` / `skill_view` / `skill_manage`）**刻意不带**——技能抽取归计划完成通道，它不靠该标记，而是显式绑定自己的工具面。
- fork 的消息只记录日志。`res["messages"]` 中的任何内容都不会进入主图。

### 2. plan extraction（todo 完成，在轮次边界）

`_detect_todo_all_complete(session_id)`（`summarization/nudges.py`）在每次压缩时评估，每个完成周期只触发一次：

- todo 存在，且每项都是 `completed` 或 `cancelled`；
- `nudge_plan_extraction_fired`（`state_register_db`）尚未置位。

它在状态跃迁时置位，并在列表并非全部完成时重置为 `False`，因此后续新的完成周期会再次触发。读取 fail-open。检测过去只在压缩时运行——从不压缩的会话永远不会抽取；现在它在**每个计划已完成的轮次**（完成钩子）运行，短会话同样会抽取。

光靠标志位看不到「就地改写已完成的 todo」，因此触发器还会比较计划**快照指纹**（`_plan_snapshot_hash`：id + content + status + plan_ref）与上次**成功**抽取后存于 `nudge_plan_snapshot_hash` 的值——内容变了就重抽、计划没变就跳过、失败的一轮会在下次完成轮重试。

当 `plan_extraction_enabled` 开启、每会话 `Summarization → nudge` 开关开启且检测触发时，钩子以 fire-and-forget 派发 `_run_plan_extraction`，在 `nudge_plan_extraction_lock` 保护下于 NUDGE 车道运行：

1. `_build_plan_context` 收集 plan 文件（优先 `plan_ref` 状态，其次第一个带 `plan_ref` 的 todo）、todo 列表以及本会话的子 agent 运行记录（`result_text` 截断到 24 KB、`outcome`、task）。无 todo 列表时返回 `{}`，调用方据此跳过。
2. 用 plan 上下文渲染 `_PLAN_EXTRACTION_PROMPT`，发给专用 agent（`_create_plan_extraction_agent`：**辅助模型**、`temperature=0`）——**完全不带对话**：数据全在 plan 文件、todo 和子 agent 运行记录里，因此这一轮既挤不爆窗口、也读不到聊天内容。其工具面显式绑定（`knowledge` / `skill_list` / `skill_view` / `skill_manage` / `memory`——最后一个是 Part 3 写 FACTS.md 的通道），技能写入只归这条通道。

该提示产出三项结果：

- **Part 1：结构化知识。** `knowledge(action="write", ...)` 把 JSON 文档写入 `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan_key>/`，按计划身份为键——见 `agent/tools/todolist/knowledge/identity.py`）：`task-<position>.json`、`wave-<index>.json`、`plan-summary.json`。每个 task 带 `failure_set`、`success_path`、`method`；wave 带失败 / 成功模式；plan 带整体方法、关键失败 / 成功与可复用模式。
- **Part 2：技能库更新。** `skill_manage` 修补已加载或既有的类级技能、新增支持文件、或在 `skills/auto/` 下创建新的类级 umbrella。提示明确要求主动（"most completed plans produce at least one skill update"），并把用户纠正、流程纠正、非平凡技巧、过时技能列为一级信号。它同时承接模块级教训：绑定具体文件/模块/测试、且该模块可预见会再次改动的教训，沉淀为 `<module>-notes` 技能（`skill_manage(action="create", name="<module>-notes", ...)`）；已存在同名技能时用 `action="patch"` 追加，绝不重复创建。内容为按模块归类的简短"现象 → 原因 → 规避动作"清单。
- **Part 3：系统级坑 → FACTS.md。** 在任何项目中都成立的坑——工具链行为、环境约束、通用命令陷阱——经 `memory` 工具以 `target="facts"` 写入；项目级事实写入 MEMORY。提示会带上当前 FACTS.md 内容与字符上限，并要求合并/替换而非追加（"宁缺毋滥"）。

后续读取由同一工具提供（`knowledge(action="read")`），精简后的 plan 摘要则由 `build_knowledge_block`（`knowledge/prompt_block.py`）自动注入系统提示。

### 3. 压缩前 memory flush

两条压缩路径（`agent/middlewares/summarization/core.py` 中的 `_apply_compression_under_lock` 与 `_aapply_compression_under_lock`）在 cut 丢弃消息时、生成摘要之前运行 flush：

- 同步路径：`run_memory_flush_sync(...)`；
- 异步路径：`await run_memory_flush(...)`。

`should_flush`（`agent/middlewares/summarization/memory_flush.py`）的门槛是 `MEMORY_FLUSH["enabled"]`，外加 `total_chars >= force_flush_chars`（50 000）或 `estimated_tokens >= soft_threshold_tokens`（8 000）。flush 是对即将丢弃文本执行一次 `llm.ainvoke` / `llm.invoke`，提示为 `_FLUSH_PROMPT`，由 `_build_llm` 以 `model=MEMORY_FLUSH["model"]`、`max_tokens=2048`、`timeout=30` 构建。它**不是** agent，也没有工具。

响应按 `§` 分隔解析条目。空结果或 `(none)` 不写任何内容。否则 `MemoryStore.append_entries` 逐条路由：匹配 `^\s*user\s*:`（大小写不敏感）的进入 USER.md，其余（Environment / Project / Decision / Tool / 无前缀）进入 MEMORY.md。`append_entries` 会针对目标文件去重，并且不同于 `add`，它通过淘汰最旧条目来保持在各自文件上限之内。

flush 只提取跨会话事实。临时任务进度有意留给摘要。任何异常都被记录并吞掉；flush 从不阻塞压缩。

### 4. 压缩后 todo fork

在同一压缩点，`_schedule_compression_todo_update`（`summarization/core.py` -> `nudges.schedule_compression_todo_update`）把 `update_todos_from_compaction` 作为 fire-and-forget `asyncio.create_task` 调度。调度器需**全部**满足：

- `compression_todo_update_enabled`（`SUMMARIZATION`，默认 `True`）；
- cut 确实丢弃了消息；
- 该会话未持有 `compression_todo_update_lock`；
- 会话有非空 todo 列表；
- 存在运行中的事件循环（同步压缩路径会跳过，并打 debug 日志）。

`update_todos_from_compaction` 运行一个 nudge agent，系统提示为 `_COMPRESSION_TODO_PROMPT`，工具恰好一个：`_build_main_session_todowrite(session_id)`。该 fork 依据被丢弃片段核对当前 todo 列表：把实际完成的项标为 `completed`，把放弃或被取代的项标为 `cancelled`，把有证据的新工作加为 `pending`，并在一次 `todowrite` 调用中写回**完整**列表（全量替换，而非增量）。若无变化，则原样写回。

fork 的结果消息只记录日志。任何内容都不会进入主图或其 checkpointer，派生会话的中间件状态在 `finally` 中清理。

## Curator（技能策展）

**frontmatter 契约（索引就是触发器）。** `<available_skills>` 只渲染技能的 frontmatter —— 正文要加载后才读 —— 所以这些字段就是路由信号：`description`（能力概述）、`when_to_use`（**必填**：这些路径创建或修补的每个技能都要写，写明应该加载它的具体场景／用户意图）与 `when_not_to_use`（可选：排除条件与替代方案）。加载器用 CJK 感知的 token 预算（60 / 50 / 50 token）渲染这两个附加标签并做 XML 转义；前端技能 tooltip 展示同样三个字段。curator 还加了更窄的第二层：伞状技能的 frontmatter 覆盖整个技能，而每个 `##` 子能力段落以自己的一行 `**When to use:**` 开头（确有排除时才加 `**When NOT to use:**`），模型 `skill_view` 后即可选对段落。它的确定性回退写出同样的形状，从合并理由里启发式提取这些条件。

Curator（`context_engine/curator/`）是维护 `skills/auto/` 技能库生命周期的后台流程——正是上文抽取路径 2 的写入目标。它只读写技能；MEMORY.md / USER.md / FACTS.md、plan 知识目录与 `todos.db` 都不在其范围内。

**是什么。** 它是空闲触发的编排器，而非定时 cron。服务入口通过 `context_engine.curator.init()` 启动守护线程（`curator-timer`）（`server/__main__.py:140`；HTTP-only 模式下跳过，`server/__main__.py:66-73`）。线程每 3600 秒唤醒一次并调用 `maybe_run_curator(idle_for_seconds=...)`（`context_engine/curator/__init__.py:122-140`）。导入该包无副作用——只有 `init()` 会启动线程（`context_engine/curator/__init__.py:151-165`）。

**何时运行。** 只有两道门都通过才会执行：

- `should_run_now()`——已启用、未暂停，且 `last_run_at` 已超过生效间隔（`context_engine/curator/transitions.py:21-39`）。生效间隔取自 `curator.interval_hours`，默认 168 小时 / 7 天（`config/sherry_settings.py:43`），客户端可通过 `.curator_state` 中的 `auto_interval_days` 覆盖为 1–5 天（`context_engine/curator/config.py:97-107`）；
- Agent 空闲时长达到 `curator.min_idle_hours`，默认 2 小时（`context_engine/curator/orchestrator.py:108-115`）。每次用户回合都会重置该空闲计时器（`server/service/messages.py:190`）。

UI 可通过 `POST /curator/run` 强制触发一次运行，它在工作线程中调用 `run_curator_review()`（`server/trigger/http/curator.py:129-160`）。归档入口就在旁边：`POST /curator/restore`（body `{"name": "<skill>"}` → `{success, message}`；未知名称、bundled/hub 遮蔽或目标已存在返回 HTTP 400）和 `GET /curator/archived`（`{success, archived, count}`），两者都由 curator 层 `restore_skill()` / `list_archived()` 支撑（`server/trigger/http/curator.py:163-204`）。

**生命周期规则。** `apply_automatic_transitions()`（`context_engine/curator/transitions.py:41-91`）遍历每个 `skills/auto/**/SKILL.md`（`context_engine/curator/usage.py:161-180`），对每个技能：跳过 pinned 技能；无活动达到 `stale_after_days`（默认 30 天）标记为 `stale`；超过 `archive_after_days`（默认 90 天）则**归档**到 `skills/.archive/` 并标记为 `archived`；重新出现活动、或处于 stale 窗口内但从未使用的技能会被重新激活。**三种移除全部可恢复**：90 天流转、合并源技能（归档时写入 `ABSORBED_INTO` 标记注明 umbrella）、被剪枝的技能，统一进入 `skills/.archive/`，`curator restore <name>` 均可拉回。恢复会在同一步把 curator 侧记录翻回 `active`（agent 侧转发器同时同步 agent 遥测记录），恢复后的技能立即重新进入 `apply_automatic_transitions`，而不会四个分支全不命中；同一秒的归档冲突得到互不嵌套的平级目录（`<skill>-<timestamp>`）。只有显式删除——`skill_manage(action="delete")` 或客户端的删除端点——才会不可逆地移除技能。归档目录不会自动清空——可用 `curator restore` 恢复或手动清理。默认值位于 `config/sherry_settings.py:42-48`。

**LLM 合并。** 当 `curator.consolidate` 开启（默认开）时，`run_curator_review()` 渲染非 pinned 技能候选列表（`context_engine/curator/review.py:69`），让主 LLM 以 temperature 0.3 把重叠的窄技能合并为类级 umbrella 技能（`CURATOR_REVIEW_PROMPT`，`context_engine/curator/review.py:16`）。新 umbrella 及其支持文件生成后写入 `skills/auto/`（`_generate_umbrella_skill`，`context_engine/curator/umbrella.py:86`；`_apply_consolidation`，`context_engine/curator/orchestrator.py:163`）。合并是该流程唯一的 LLM 步骤；失败会被捕获，运行继续。

**与四条抽取路径的关系。** 路径 2（压缩时 plan extraction）是生产者：它经 `skill_manage` 创建或修补 `skills/auto/`。Curator 是同一产出的下游维护者——对 plan extraction 创建的技能做流转、合并与修剪。另外三条路径从不触碰 `skills/auto/`（分别写 MEMORY.md / USER.md / FACTS.md、plan 知识目录、`todos.db`），因此与 Curator 没有交集。

**状态与边界。** 运行状态存于 `skills/.curator_state`（`context_engine/curator/constants.py:3`，由 `context_engine/curator/state.py` 读写）；每次运行在 `logs/curator/{timestamp}/` 下写出 `run.json` + `REPORT.md`（`context_engine/curator/constants.py:4`；`context_engine/curator/report.py:196-205`）。范围仅限 `skills/auto/`——绝不触碰内置技能，pinned 技能跳过所有破坏性流转，整个流程在后台线程运行，不占对话回合路径。完整细节：[curator/README.md](../../context_engine/curator/README.zh.md)。

## 隔离与安全

| 防护 | 保护内容 |
|---|---|
| nudge metadata 白名单（`metadata["nudge"]`，`_NudgeLimitTool` 默认规则） | memory nudge 与 plan extraction 只能调用带 nudge 阶段标记的工具。其它任何主工具都返回错误 `ToolMessage`，不执行。 |
| 压缩 metadata 白名单（`metadata["todo_update"] is True`，`_NudgeLimitTool(allowed_metadata_key="todo_update")`） | 压缩 fork 恰好只放行带该 metadata 标记的 `todowrite` 垫片。 |
| 派生会话键 `<id>::compression-todo` | 压缩 fork 的 `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 状态键不会与主会话冲突，因为 fork 运行期间主会话正处在 `awrap_model_call` 中。只有 `compression_todo_update_lock` 有意写在主会话上，作为跨路径防重入协调器。 |
| 绑定主会话的 `todowrite` 垫片 | fork 图运行在派生键下，因此由状态注入的真实 `todowrite` 会解析到错误会话。垫片逐字复用真实工具的 `args_schema` 与 `description`（零 schema 漂移），丢弃注入的 `session_id`，并绑定构建时捕获的主会话 id。 |
| 只读 fork、无 checkpointer | 每个 nudge / 抽取 fork 的结果消息都只记录并丢弃。fork 无 checkpointer，无法写主图状态。 |
| 每会话防重入锁 | `nudge_review_memory_lock`、`nudge_plan_extraction_lock`、`compression_todo_update_lock` 阻止同一抽取路径重叠运行。任一 nudge 锁被持有时，压缩调度器完全跳过派发（不排队）。 |
| fail-open 边界 | 每条路径都用 `try/except` 包裹工作、记录日志并返回。任何抽取失败都不会传播进回合、压缩或其它抽取。 |
| 后台任务引用保留 | `_COMPRESSION_TODO_TASKS` 集合持有强引用，防止 asyncio 回收在途任务。 |

## 存储与上限

| 存储 | 路径（常量） | 上限 |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"`（`workspace/memory/MEMORY.md`） | 2200 字符（`MEMORY_TOOL["memory_char_limit"]`） |
| USER.md | `config.path.MEMORY_DIR / "USER.md"`（`workspace/memory/USER.md`） | 1375 字符（`MEMORY_TOOL["user_char_limit"]`） |
| FACTS.md | `config.path.MEMORY_DIR / "FACTS.md"` = `workspace/memory/FACTS.md`（唯一全局文件；四语模板在 `workspace/template/<lang>/FACTS.md`） | 1375 字符（`MEMORY_TOOL["facts_char_limit"]`）；`add` 超限时滚动淘汰最旧条目 |
| plan 知识 | `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan_key>/`——按身份为键） | 每个 plan 有 `task-<n>.json`、`wave-<n>.json`、`plan-summary.json`；字段上限由提示约束（150 / 100 字符） |
| 技能 | `config.path.AUTO_SKILLS_DIR`（`skills/auto/`） | `SKILL.md` 及 `references/`、`templates/`、`scripts/` 支持文件 |
| todos | `agent/tools/todolist/data/todos.db`（`store_sqlite._DB_PATH`） | 会话作用域列表，全量替换写入 |

## 配置开关

| 键 | 位置 | 默认值 | 作用 |
|---|---|---|---|
| `MEMORY_FLUSH_ENABLED` | env -> `MEMORY_FLUSH["enabled"]`（`config/features/agent_side/memory_flush.py`） | `1`（开） | 压缩前 flush 总开关 |
| `MEMORY_FLUSH_MODEL` | env -> `MEMORY_FLUSH["model"]` | `""`（用工厂默认） | flush 使用的廉价抽取模型 |
| `soft_threshold_tokens` | `MEMORY_FLUSH` | `8000` | 被丢弃片段达到此规模即 flush |
| `force_flush_chars` | `MEMORY_FLUSH` | `50000` | 字符数超过此值无条件 flush |
| `output_max_tokens` | `MEMORY_FLUSH` | `2048` | flush 调用输出上限 |
| `timeout_seconds` | `MEMORY_FLUSH` | `30` | flush 调用超时 |
| `compression_todo_update_enabled` | `SUMMARIZATION`（`config/features/agent_side/summarization.py`） | `True` | 启用压缩后 todo fork |
| `plan_extraction_enabled` | `NUDGE`（`config/features/agent_side/nudge.py`） | `True` | 启用 todo 完成时的 plan extraction |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 实际压缩后的主动压缩冷却 |
| `memory_char_limit` / `user_char_limit` / `facts_char_limit` | `MEMORY_TOOL`（`config/features/agent_side/memory_tool.py`） | `2200` / `1375` / `1375` | MEMORY.md / USER.md / FACTS.md 上限 |

## 验证

定向测试：

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_nudges.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/system_prompt/test_plan_extraction.py \
    tests/agent/middlewares/test_plan_extraction_module_notes.py \
    tests/agent/tools/test_memory_store.py \
    tests/agent/tools/test_memory_store_facts.py -q
```

- `test_compression_todo_update.py`：触发门槛、fire-and-forget 调度、防重入锁、fail-open 释放、提示内容、`todo_update` metadata 门禁，以及完整 fork 隔离（派生键、主会话 `todowrite` 垫片、无 checkpointer / 消息泄漏）。
- `test_compression_nudges.py`：压缩时派发（memory review 从 compact 接缝触发；无切点压缩不派发）。`test_memory_review_digest.py` 钉住被丢弃段的接线、cache_warm 的模型重建（descriptor 字段、环境默认回退、会话工具面），以及 `memory_review_mode="digest"` 下的折叠。持久化断言位于 `tests/agent/middlewares/message_persistence/`。
- `test_compression_cooldown_persist.py`：冷却跨重启存活。
- `test_memory_flush.py`：flush 门槛、路由与非阻塞失败。
- `test_plan_extraction.py`：`_detect_todo_all_complete` 四个分支、`schedule_memory_review` 的每次压缩派发记忆回顾与锁语义，以及 `_build_plan_context`。
- `test_plan_extraction_module_notes.py`：Part 2 的 `<module>-notes` carry-forward 指引与 Part 3 的 FACTS 指令、两个 nudge 提示中的实时 FACTS.md 渲染，以及假 LLM 行为测试（create 落到 `skills/auto/<module>-notes/`；同名已存在则 patch，绝不重复创建）。
- `test_memory_store.py`：MEMORY.md / USER.md 存储语义。
- `test_memory_store_facts.py`：`facts` target——add/replace/remove、超限滚动淘汰最旧、原子写、加载时创建文件、实时内容渲染与提示快照注入（空文件不产生空块）。

AI 评判评估：`evals/nudge_extraction/suite.py` 构造一次已完成 plan 的运行（plan 文件、已完成 todos、合成的子 agent 运行记录），调用真实的 `_run_plan_extraction`，再让辅助 LLM judge 判断产出的技能是否真正扎根于本次运行、可复用且非泛泛而谈。所有写入都重定向进沙箱，真实 `skills/auto/` 与 `workspace/` 绝不被触碰。

```bash
uv run python evals/evals.py nudge_extraction
```

该套件检查 `knowledge_written`、`skills_created`、`ai_judge_skill_quality` 与 `no_repo_pollution`。

## 已知边界

- **压缩 fork 有意不给 `todoread`。** 只放行带 `todo_update` 标记的 `todowrite` 垫片；fork 在提示中已收到当前列表，`todoread` 有意不打标记（`agent/tools/todolist/tools/__init__.py`）。
- **memory flush 只提取跨会话事实。** 临时任务进度归摘要，不进 MEMORY.md / USER.md。flush 也不写 FACTS.md——该文件由 memory review / plan extraction nudge 维护。
- **FACTS.md 是单文件记忆。** 它与 MEMORY.md / USER.md 同机制（单文件、`§` 分隔条目、原子改名写入、每文件字符上限），并注入每次系统提示；它不是分层/分类存储，超出上限时最旧条目滚动淘汰。
- **冷却只抑制主动压缩。** `compaction_cooldown_rounds`（3）在实际压缩后阻止 T1 / T2 / T3 压缩；T4 / T5 提供方错误恢复在构造上绕过冷却、每回合尝试上限等反抖动门槛。

## 相关文档

- [会话级内存架构](../session_memory/README.md)：压缩前 memory flush。
- [摘要压缩](../summarization/README.md)：压缩触发，以及门控 flush 与 todo fork 的冷却。
- [长程任务](../long-running-tasks/README.md)：TaskFlow，以及被 plan extraction 读取的 todo 规划层。
- [中间件 README](../../agent/middlewares/README.md)：`@dynamic_prompt` 系统提示词注入与 Summarization 参考。
- [Curator README](../../context_engine/curator/README.zh.md)：上文所述的后台技能维护编排器。
