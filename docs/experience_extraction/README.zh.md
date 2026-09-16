# 经验抽取架构

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

本文梳理 Agent 在运行过程中**何时**抽取经验、**以何种机制**抽取、以及经验**写到哪里**。生命周期中共接入五条抽取路径：每回合 facts 管线、每 10 回合 memory nudge、todo 全部完成时的 plan extraction、压缩前 memory flush、压缩后 todo fork。

> 下文每条断言均已对照源码核实。符号名、配置键、默认值与路径均真实存在于 `agent/middlewares/`、`context_engine/facts/`、`agent/tools/`、`config/features/` 的代码中。

## 设计原则

1. **所有抽取都扩展既有存储。** 产出分别落到 MEMORY.md / USER.md、分层 `facts/*.md`、plan 知识目录、`skills/auto/`、或 `todos.db`。没有任何抽取路径另建平行存储。
2. **全部 fail-open。** 每个触发点都记录并吞掉自身失败。todo 存储损坏、plan 文件不可读、LLM 调用失败、游标损坏，都不会阻塞或打断主对话回合。
3. **回合路径零阻塞。** facts 管线与压缩后 todo fork 都是 fire-and-forget 后台任务。memory nudge 与 plan extraction 作为独立子 agent 运行（异步路径中通过 `asyncio.gather` 与持久化并发）。
4. **机制按需匹配。** 需要工具调用的工作才用完整 `create_agent` fork（memory nudge、plan extraction、todo fork）。纯抽取（每回合 facts、memory flush）只用一次辅助 LLM 调用。

## 触发 × 机制 × 落点

| 触发 | 机制（是否 fork agent / 调用形态） | 写入目标 |
|---|---|---|
| 每回合结束 | facts 管线（`context_engine/facts/`）：先入队该回合，再跑辅助 LLM 提取器，非 agent。plan 抽取回合会跳过，由该回合自身的 pass 吸收待处理区间。 | 经 `TieredMemoryStore.add_fact` 写入 `facts/<category>.md` |
| 每 10 回合（`nudge_memory_threshold`） | memory nudge（`_nudge_memory`）：fork 一个 `create_agent` nudge agent，使用 `_MEMORY_REVIEW_PROMPT` | 经 `memory` 工具写入 MEMORY.md / USER.md |
| todo 列表变为全部完成（`completed` / `cancelled`） | plan extraction（`_nudge_plan_extraction`）：fork 一个 nudge agent，使用 `_PLAN_EXTRACTION_PROMPT` | ① 知识 JSON ② `skills/auto/` ③ `facts/*.md` |
| 压缩前（cut 确实丢弃了消息） | memory flush（`run_memory_flush[_sync]`）：一次廉价 LLM 调用，非 agent | 经 `MemoryStore.append_entries` 写入 MEMORY.md 与 USER.md |
| 压缩后（cut 确实丢弃了消息） | todo fork（`update_todos_from_compaction`）：fire-and-forget nudge agent，使用 `_COMPRESSION_TODO_PROMPT` | 经绑定主会话的 `todowrite` 垫片写入 `todos.db` |

## 触发详情

### 1. 每回合 facts 管线（session-memory P2-3）

`ContextEngineHook.aafter_agent`（`agent/middlewares/context_engine/core.py`）先把最后一回合持久化到 MesMemory，随后在 `turn_num > 0` 且当前回合**不是** plan 抽取回合时，为 `_run_facts_pipeline(session_id, turn_num)` 创建一个后台任务。

`_run_facts_pipeline` 依次调用 `enqueue_turn` 与 `process_pending`（`context_engine/facts/queue.py`）：

- `enqueue_turn` 把 `enqueued` 水位（`facts_cursor_enqueued`）推进到已持久化的回合号。
- `process_pending` 读取两个水位之间的待处理区间，格式化对话行，并用辅助 LLM 调用 `extract_facts`（`context_engine/facts/extractor.py`）。每个 `{"category", "fact"}` 项经 `TieredMemoryStore.add_fact` 写入，之后才推进 `consumed` 水位（`facts_cursor_consumed`）。

游标是**双水位**（`context_engine/facts/cursor.py`）：两个值单调且持久化于 `state_register_db`，因此入队与消费之间发生崩溃会重放该区间，而不是丢数据。这是 at-least-once 语义：重放的回合可能产生重复事实，由 `add_fact` 按精确文本去重。

**plan 抽取回合的让位。** plan extraction 触发时，每回合管线不启动。`_nudge_plan_extraction` 在同一次 LLM pass 中覆盖待处理区间（下文 Part 3），且仅在成功后才推进 consumed 水位，所以同一回合永远不会跑两个提取器，也不会丢失区间。

### 2. 每 10 回合 memory nudge

`ContextEngineHook._after_agent_impl` 每回合在 `state_register_db` 中递增 `nudge_review_memory_count`。计数达到 `nudge_memory_threshold`（默认 10）时，计数重置为 0，并在 `nudge_review_memory_lock`（`state_register_mem`）保护下运行 `_nudge_memory(session_id, system_prompt, messages)`。任一 nudge 锁被持有时，`after_agent` 跳过 nudge 决策（计数照常递增）。

`_nudge_memory`（`agent/middlewares/context_engine/nudge.py`）通过 `_create_nudge_agent` 构建 nudge agent，并把 `_MEMORY_REVIEW_PROMPT` 作为 `HumanMessage` 追加到对话后调用。该提示要求 agent 用 `memory` 工具保存持久的用户特征（persona、偏好、个人细节）与行为期望；若无内容可存，则回复 "Nothing to save." 并停止。

- nudge agent 是主 LLM 上独立的 `create_agent`，中间件为 `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`，无 checkpointer。
- `_NudgeLimitTool`（未指定 `allowed_metadata_key`）只放行 metadata 带 `nudge: True` 的工具。`memory`、`skill_list`、`skill_view`、`skill_manage`、`knowledge` 都带该标记，因此 nudge agent 能写 memory，但无法调用任意主工具。
- fork 的消息只记录日志。`res["messages"]` 中的任何内容都不会进入主图。

### 3. todo 全部完成时的 plan extraction

`_detect_todo_all_complete(session_id)`（`core.py`）每个完成周期只触发一次：

- todo 存在，且每项都是 `completed` 或 `cancelled`；
- `nudge_plan_extraction_fired`（`state_register_db`）尚未置位。

它在状态跃迁时置位，并在列表并非（或不再是）全部完成时重置为 `False`，因此后续新的完成周期会再次触发。读取 fail-open。

当 `plan_extraction_enabled` 开启且检测触发时，`_nudge_plan_extraction` 在 `nudge_plan_extraction_lock` 保护下运行：

1. `_build_plan_context` 收集 plan 文件（优先 `plan_ref` 状态，其次第一个带 `plan_ref` 的 todo）、todo 列表、start-work ledger（`.omo/start-work/ledger.jsonl`）以及本会话的子 agent 运行记录（`result_text` 截断到 24 KB、`outcome`、task）。无 todo 列表时返回 `{}`，调用方据此跳过。
2. `_fetch_pending_facts` 为 Part 3 渲染尚未消费的 facts 区间。
3. 用 plan 上下文与 facts 片段渲染 `_PLAN_EXTRACTION_PROMPT`，连同对话一起发给 nudge agent（同一构建器、同一 `nudge: True` 门禁）。
4. `ainvoke` 成功后，若存在待处理 facts 区间，`_advance_facts_consumed` 推进 consumed 水位。

该提示产出三类结果：

- **Part 1：结构化知识。** `knowledge(action="write", ...)` 把 JSON 文档写入 `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan-name>/`）：`task-<position>.json`、`wave-<index>.json`、`plan-summary.json`。每个 task 带 `failure_set`、`success_path`、`method`；wave 带失败 / 成功模式；plan 带整体方法、关键失败 / 成功与可复用模式。
- **Part 2：技能库更新。** `skill_manage` 修补已加载或既有的类级技能、新增支持文件、或在 `skills/auto/` 下创建新的类级 umbrella。提示明确要求主动（"most completed plans produce at least one skill update"），并把用户纠正、流程纠正、非平凡技巧、过时技能列为一级信号。
- **Part 3：持久事实。** `memory(action="fact_add", target="<category>", content="<fact>")` 写入 `facts/*.md`。该块仅在存在待处理 facts 区间时渲染；它提取用户偏好、项目约定、关键决策与工具经验，绝不提取临时进度。

后续读取由同一工具提供（`knowledge(action="read")`），精简后的 plan 摘要则由 `build_knowledge_block`（`knowledge/prompt_block.py`）自动注入系统提示。

### 4. 压缩前 memory flush（session-memory P0-1）

两条压缩路径（`agent/middlewares/summarization/core.py` 中的 `_apply_compression_under_lock` 与 `_aapply_compression_under_lock`）在 cut 丢弃消息时、生成摘要之前运行 flush：

- 同步路径：`run_memory_flush_sync(...)`；
- 异步路径：`await run_memory_flush(...)`。

`should_flush`（`agent/middlewares/summarization/memory_flush.py`）的门槛是 `MEMORY_FLUSH["enabled"]`，外加 `total_chars >= force_flush_chars`（50 000）或 `estimated_tokens >= soft_threshold_tokens`（8 000）。flush 是对即将丢弃文本执行一次 `llm.ainvoke` / `llm.invoke`，提示为 `_FLUSH_PROMPT`，由 `_build_llm` 以 `model=MEMORY_FLUSH["model"]`、`max_tokens=2048`、`timeout=30` 构建。它**不是** agent，也没有工具。

响应按 `§` 分隔解析条目。空结果或 `(none)` 不写任何内容。否则 `MemoryStore.append_entries` 逐条路由：匹配 `^\s*user\s*:`（大小写不敏感）的进入 USER.md，其余（Environment / Project / Decision / Tool / 无前缀）进入 MEMORY.md。`append_entries` 会针对目标文件去重，并且不同于 `add`，它通过淘汰最旧条目来保持在各自文件上限之内。

flush 只提取跨会话事实。临时任务进度有意留给摘要。任何异常都被记录并吞掉；flush 从不阻塞压缩。

### 5. 压缩后 todo fork

在同一压缩点，`_schedule_compression_todo_update`（`summarization/core.py` -> `nudge.schedule_compression_todo_update`）把 `update_todos_from_compaction` 作为 fire-and-forget `asyncio.create_task` 调度。调度器需**全部**满足：

- `compression_todo_update_enabled`（`SUMMARIZATION`，默认 `True`）；
- cut 确实丢弃了消息；
- 该会话未持有 `compression_todo_update_lock`；
- 会话有非空 todo 列表；
- 存在运行中的事件循环（同步压缩路径会跳过，并打 debug 日志）。

`update_todos_from_compaction` 运行一个 nudge agent，系统提示为 `_COMPRESSION_TODO_PROMPT`，工具恰好一个：`_build_main_session_todowrite(session_id)`。该 fork 依据被丢弃片段核对当前 todo 列表：把实际完成的项标为 `completed`，把放弃或被取代的项标为 `cancelled`，把有证据的新工作加为 `pending`，并在一次 `todowrite` 调用中写回**完整**列表（全量替换，而非增量）。若无变化，则原样写回。

fork 的结果消息只记录日志。任何内容都不会进入主图或其 checkpointer，派生会话的中间件状态在 `finally` 中清理。

## 隔离与安全

| 防护 | 保护内容 |
|---|---|
| nudge metadata 白名单（`metadata["nudge"]`，`_NudgeLimitTool` 默认规则） | memory nudge 与 plan extraction 只能调用带 nudge 阶段标记的工具。其它任何主工具都返回错误 `ToolMessage`，不执行。 |
| 压缩 metadata 白名单（`metadata["todo_update"] is True`，`_NudgeLimitTool(allowed_metadata_key="todo_update")`） | 压缩 fork 恰好只放行带该 metadata 标记的 `todowrite` 垫片。 |
| 派生会话键 `<id>::compression-todo` | 压缩 fork 的 `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 状态键不会与主会话冲突，因为 fork 运行期间主会话正处在 `awrap_model_call` 中。只有 `compression_todo_update_lock` 有意写在主会话上，作为跨路径防重入协调器。 |
| 绑定主会话的 `todowrite` 垫片 | fork 图运行在派生键下，因此由状态注入的真实 `todowrite` 会解析到错误会话。垫片逐字复用真实工具的 `args_schema` 与 `description`（零 schema 漂移），丢弃注入的 `session_id`，并绑定构建时捕获的主会话 id。 |
| 只读 fork、无 checkpointer | 每个 nudge / 抽取 fork 的结果消息都只记录并丢弃。fork 无 checkpointer，无法写主图状态。 |
| 每会话防重入锁 | `nudge_review_memory_lock`、`nudge_plan_extraction_lock`、`compression_todo_update_lock` 阻止同一抽取路径重叠运行。任一 nudge 锁被持有时，`after_agent` 跳过 nudge 决策。 |
| fail-open 边界 | 每条路径都用 `try/except` 包裹工作、记录日志并返回。任何抽取失败都不会传播进回合、压缩或其它抽取。 |
| 后台任务引用保留 | `_BACKGROUND_TASKS`、`_COMPRESSION_TODO_TASKS` 集合持有强引用，防止 asyncio 回收在途任务。 |

## 存储与上限

| 存储 | 路径（常量） | 上限 |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"`（`workspace/memory/MEMORY.md`） | 2200 字符（`MemoryStore.memory_char_limit`） |
| USER.md | `config.path.MEMORY_DIR / "USER.md"`（`workspace/memory/USER.md`） | 1375 字符（`MemoryStore.user_char_limit`） |
| facts 层 | `config.path.FACTS_DIR`（`workspace/memory/facts/<category>.md`） | 每文件 4000 字符，5 个类别：`environment`、`project`、`decisions`、`user_prefs`、`tool_lessons`；仅按需读取，绝不注入系统提示 |
| plan 知识 | `config.path.PLAN_KNOWLEDGE_DIR`（`workspace/knowledge/plans/<plan>/`） | 每个 plan 有 `task-<n>.json`、`wave-<n>.json`、`plan-summary.json`；字段上限由提示约束（150 / 100 字符） |
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
| `plan_extraction_enabled` | `CONTEXT_ENGINE_HOOK`（`config/features/agent_side/context_engine_hook.py`） | `True` | 启用 todo 完成时的 plan extraction |
| `nudge_memory_threshold` | `CONTEXT_ENGINE_HOOK` | `10` | memory nudge 间隔回合数 |
| `facts_char_limit` | `TIERED_MEMORY`（`config/features/agent_side/tiered_memory.py`） | `4000` | 单类别 facts 文件上限 |
| `facts_categories` | `TIERED_MEMORY` | `environment`、`project`、`decisions`、`user_prefs`、`tool_lessons` | 固定类别集 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 实际压缩后的主动压缩冷却 |
| `memory_char_limit` / `user_char_limit` | `MemoryStore.__init__` | `2200` / `1375` | MEMORY.md / USER.md 上限 |

## 验证

定向测试：

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/context_engine/test_plan_extraction.py \
    tests/context_engine/facts/test_facts_extraction.py \
    tests/agent/tools/test_memory_store.py -q
```

- `test_compression_todo_update.py`：触发门槛、fire-and-forget 调度、防重入锁、fail-open 释放、提示内容、`todo_update` metadata 门禁，以及完整 fork 隔离（派生键、主会话 `todowrite` 垫片、无 checkpointer / 消息泄漏）。
- `test_compression_cooldown_persist.py`：冷却跨重启存活。
- `test_memory_flush.py`：flush 门槛、路由与非阻塞失败。
- `test_plan_extraction.py`：`_detect_todo_all_complete` 四个分支、`after_agent` 五元组契约、nudge 派发、facts 让位，以及 `_build_plan_context`。
- `test_facts_extraction.py` 与 `test_memory_store.py`：提取器解析 / 重放语义，以及两个 memory 存储。

AI 评判评估：`evals/nudge_extraction/suite.py` 构造一次已完成 plan 的运行（plan 文件、已完成 todos、合成的子 agent 运行记录、一个待处理 facts 回合），调用真实的 `_nudge_plan_extraction`，再让辅助 LLM judge 判断产出的技能是否真正扎根于本次运行、可复用且非泛泛而谈。所有写入都重定向进沙箱，真实 `skills/auto/` 与 `workspace/` 绝不被触碰。

```bash
uv run python evals/evals.py nudge_extraction
```

该套件检查 `knowledge_written`、`facts_written`、`skills_created`、`ai_judge_skill_quality` 与 `no_repo_pollution`。

## 已知边界

- **压缩 fork 有意不给 `todoread`。** 只放行带 `todo_update` 标记的 `todowrite` 垫片；fork 在提示中已收到当前列表，`todoread` 有意不打标记（`agent/tools/todolist/tools/__init__.py`）。
- **memory flush 只提取跨会话事实。** 临时任务进度归摘要，不进 MEMORY.md / USER.md。
- **facts 抽取是 at-least-once。** 空事实列表是合法结果，仍推进 consumed 水位；非列表的 LLM 响应会抛错，水位不动，区间重放。重放可能产生重复事实，由 `add_fact` 按精确文本去重。
- **冷却只抑制主动压缩。** `compaction_cooldown_rounds`（3）在实际压缩后阻止 T1 / T2 / T3 压缩；T4 / T5 提供方错误恢复在构造上绕过冷却、每回合尝试上限等反抖动门槛。
- **plan extraction 吸收 facts 区间。** plan 抽取回合跳过每回合管线，由 Part 3 在同一次 pass 覆盖待处理区间。仅当区间确实被注入且该 pass 成功时，consumed 水位才推进。

## 相关文档

- [会话级内存架构](../session_memory/README.md)：SESSION 计划中的 P2-3（facts）与 P0-1（memory flush）。
- [摘要压缩](../summarization/README.md)：压缩触发，以及门控 flush 与 todo fork 的冷却。
- [长程任务](../long-running-tasks/README.md)：TaskFlow，以及被 plan extraction 读取的 todo 规划层。
- [中间件 README](../../agent/middlewares/README.md)：ContextEngineHook 与 Summarization 参考。
