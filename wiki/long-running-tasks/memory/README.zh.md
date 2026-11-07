# 🧠 记忆与连续性

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Long-Running Tasks](../README.zh.md) 的一部分：双层记忆系统、摘要 ↔ TaskFlow 协调、子 Agent 记忆回流、工具输出单行摘要、会话连续性与 TaskFlow 自动恢复。

---

## 🧠 分层记忆

两层，区别在于*如何*抵达模型：

| 层 | 存储 | 位置 | 是否进入提示词？ |
| :--- | :--- | :--- | :--- |
| **L1 —— 精炼记忆** | `memory_entries` 行（`memory` Agent 笔记、`user` 用户画像、`facts` 系统坑） | MesMemory SQLite（`mes_memory.db`）的 `memory_entries` 表 | 是——冻结快照，始终注入 |
| **L2 —— 原始历史** | `mes_memory.db`（SQLite、WAL、FTS5） | `src/store/mes_memory/mes_memory.db` | 否——由 `context_engine` / `message_search` 检索 |

一个条目是 `memory_entries` 中的**一行 SQLite 记录**；`§` 分隔符只保留在遗留导入与 `GET/PUT /memory` 兼容路径中。条目可以跨多行，没有 YAML frontmatter，也没有项目符号前缀。

第 1 层由 `MemoryStore` 管理（`agent/tools/memory.py::load_from_store`）：每个目标的字符上限为 `2200`（memory）、`1375`（user）和 `1375`（facts），注入扫描（`_MEMORY_THREAT_PATTERNS`）会拒绝提示词注入与凭证外泄内容，配合精确匹配去重；并发安全由 SQLite WAL 提供，不再使用文件锁和原子重写。行上记录 `origin_session`、`usage_count` 和 `last_used`；渲染块仅在来源已知时追加 `[origin: …, created: …, used: Nx]`，`memory(action="read")` 会累加使用次数。实时行会立即改动，而提示词使用的是一份在 `load_from_store()` 时捕获的**冻结快照**，以保持会话期内前缀缓存稳定。遗留的 `MEMORY.md`/`USER.md`/`FACTS.md` 会在启动时导入一次（`context_engine/store/memory_migration.py`）并改名为 `*.md.bak`；只要表里已有任意一行，导入即跳过。

`memory` 工具被打上 `scope="main_only"`，因此子 Agent 永远看不到它。

**图状态检查点存储。** 会话的 LangGraph 状态还会持久化到 `src/checkpoints/sqlite.db`，独立于上面两层：每次 `built_agent()` 调用都会把它剪枝为每线程最新检查点（`ThreadSafeAsyncSqliteSaver.aclean_old_checkpoints`，`agent/core.py:227`）；由于 `auto_vacuum=0`，DELETE 只释放页面而不缩小文件，因此同一调用在剪枝后立即读取 `PRAGMA freelist_count × page_size`，仅当释放的空间超过 `_VACUUM_THRESHOLD_BYTES`（10 MB，`agent/checkpointer/thread_safe_checkpointer.py`）时才执行 `VACUUM`——失败开放：VACUUM 报错只记录日志，剪枝结果不受影响。

## 🔎 语义召回与整理

两者都是可选开关，且全程 fail-open。开启 `semantic_recall_enabled`（记忆设置里的“语义召回”）后，系统提示词只放紧凑索引（`MemoryStore.format_index_for_system_prompt`），每轮由 `memory_recall` reminder（priority 88，cooldown 3）注入与当前请求相关的条目。`context_engine/store/memory_recall.py` 会用 bge-m3 自愈 `memory_entries.embedding` 索引，用 bge-reranker 精排 cosine 候选，按 `semantic_recall_char_budget` 截断，跳过早于当前 `context_epoch` 基线的条目，并累加 `usage_count`。`memory(action="recall")` 与 `POST /memory/search` 走同一路径；开关关闭时搜索回退到子串匹配。

`POST /memory/consolidate` 启动异步任务（`GET /memory/consolidate/:task_id` 查询）：先跑确定性的 `prune_unused` 老化，再在活跃条目达到 `consolidation_min_entries`（10）时，用主模型通过 memory 工具合并重复、消解矛盾、纠正错放目标并精简措辞。压缩在记忆 review 之后跑同一 pass（15 turns）；会话删除在两波 salvage 之后跑（5 turns）。失败只记日志，不阻塞压缩或删除。

## 🔗 摘要 ↔ TaskFlow 协调

当压缩构建其 LLM 提示词时，`_get_taskflow_context_sync(session_id)`（`agent/middlewares/summarization/core.py:122`）会渲染本会话的活动 flow，并把它作为摘要提示词的**最后**一部分追加（`_build_summary_prompt`，`summarization/summary_generation.py:554`）：

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

该区块以 `## Current TaskFlow State (authoritative)` 为标题（`summarization/core.py:137`），对于本会话拥有的至多三个 flow（存储读取在 SQL 层按 `session_id` 过滤，无 Python 层二次过滤），列出 flow id/状态、描述、`done/total` 进度与状态分解、最后两个已完成的步骤、前两个待处理步骤，以及任何等待原因。它复用了 DAG 辅助函数 `step_status` 与 `steps_summary`，并且完全失败开放（`except Exception → ""`）。确定性回退摘要（`_build_static_fallback_summary`）**不**包含该区块；它只是 LLM 提示词的补充。

## 🧠 子 Agent 记忆回流

`SubagentCompletionDrainMiddleware`（`agent/middlewares/subagent_completion_drain/core.py`）是排队的子 Agent 完成消息在父回合的摄入点：在 `before_model` 时，它会重新水合并排空会话的 `SteeringQueue`，注入重建好的完成载体消息。**当排空非空时**，它还会把共享记忆与父 Agent 的内存视图做一次对账：

```python
# subagent_completion_drain/core.py:93-117
def _backflow_shared_memory() -> None:
    from agent.tools.memory import memory_store
    memory_store.load_from_store()
```

父 Agent 与子 Agent 共享**同一个进程级 `MemoryStore`**（底层是 SQLite），因此子 Agent 的写入已经持久化。可能发生漂移的是父 Agent 的内存视图——实时行，以及构建系统提示词时用的那份**冻结快照**。重载既是关键也是充分的：SQLite 是唯一事实来源，没有回写步骤，重载也不会覆盖并发写入者（旧的 load → persist 对账会）。

与排空一样，回流也是**失败开放**的——记忆 I/O 失败会被记录并吞掉，完成载体仍会抵达父回合。排空会让父 Agent 对完成保持警惕：会话没有通过证据时，它会在载体之后追加一条强制校验门控消息，提醒父 Agent：完成只是一份 `DoneClaim`，而不是已验证的结果（在把待办标记为完成之前，先用 `todoread` 校验、对照验收标准，并排查陈旧状态）。

## ✂️ 工具输出摘要

在非 LLM 剪枝过程中，较大的旧 `ToolMessage` 内容通常被清成一个标记。`pub/func/message/tool_output_prune.py` 用一行**工具专属摘要**替换了裸标记 `_PRUNE_MARKER = "[Old tool result content cleared]"`（`tool_output_prune.py:22`），让模型仍能保留关于该结果内容的线索：

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_flow"      -> "[taskflow_flow] {len} chars"
"taskflow_step"      -> "[taskflow_step] {len} chars"
"taskflow_inspect"   -> "[taskflow_inspect] {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`（`tool_output_prune.py:104`）从最新到最旧遍历消息，遇到摘要消息即停止，保护最新的 `prune_protect_tokens`（40 000）个 token，跳过受保护工具（`{"memory", "skill_view", "skill_list"}`），并且只有当释放的 token 达到 `prune_min_reduction_tokens`（5 000）时才提交。被替换的消息是 `model_copy` 克隆，携带 `additional_kwargs["status"] = "compacted"` 与 `["original_length"]`。摘要上限为 200 字符；任何模板异常都会回退到标记。它由 `Summarization._run_non_llm_strategies` 调用（`summarization/compression.py:314`）。

## 🔄 会话连续性

会话被清除时，`context_engine/session_continuity.py` 会持久化一份结束状态，供下一个会话提供连续性。`server/service/session_cleanup_service.py::purge_session` 把 `auto_save_on_session_end(session_id)` 作为**第 0 步**，在任何删除之前调用（该函数第 0 步）。该函数会：

1. 通过 `runtime.session.relation_register` 解析 `channel_id`/`chat_id`（`_get_channel_chat_for_session`，`session_continuity.py:167`）。
2. 读取最近 3 个回合，并把最后一条 AI 回复裁剪到 `_MAX_SUMMARY_CHARS = 500`（`session_continuity.py:28`）。
3. 收集该会话的活动 flow id。
4. 调用 `save_session_end_state(...)`，写入 `src/data/session_continuity/{safe-key}.json`（`session_continuity.py:25`），字段为 `last_session_id`、`ended_at`、`ended_ts`、`summary`、`taskflow_ids`。

在删除消息存储之后，`clear_session` 还会清理该会话的**计划存储**——`agent.tools.todolist.registry.store_sqlite.delete_todos_by_session(session_id)` 与 `agent.tools.taskflow.registry.store_sqlite.delete_flows_by_session(session_id)`——因此被清除的会话不会留下任何 todo 或任务流残留。删除是尽力而为的（失败会记录日志，绝不阻塞其余清理），只删除 `session_id` 匹配的行，且隔离前的任务流行（`session_id = ''`）永远不会被匹配。

下一个会话通过 `build_continuity_prompt(session_id)`（`session_continuity.py:80`）读取它，它由 `workspace/prompt_builder.py:169` 的 `_build_continuity_block` 调用，并在构建完整提示词时注入（`prompt_builder.py:289-295`）：

```
## Last Session (continuity)
Last conversation ended with: <摘要 ≤ 500 字符>
Related tasks: <至多 3 个 flow id>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

会话永远不会收到自己的状态（`last_session_id == session_id → ""`）。由于查询同时要求 **channel id 与 chat id**，没有渠道绑定的纯 WebSocket 会话拿不到连续性区块。存储是文件系统上的 JSON（以 `channel:chat` 为键，退化时以 `session_id` 为键），而不是数据库。

## 🧹 会话删除时的记忆抢救

销毁主会话前，`purge_session` 还会调度一次独立的 memory review（`background_reviews.salvage_session_memory`）。它快照压缩后的 checkpoint transcript、模型、系统提示词和工具面，只读计算正常压缩切割线，调度后立即返回；删除不等待模型。第一波用捕获的（cache-warm）模型/提示词/工具面复盘本会被压缩丢弃的前缀；写完记忆条目（来源戳为被删除会话的 id，而不是派生的 wave 键）后，第二波重新加载 memory、非持久化重建系统提示词，并用仅 `memory` 工具面复盘保留的尾部。两波都在 BACKGROUND_REVIEW 车道运行，使用派生会话键（`<id>::session-end-salvage:1|2`），无 checkpointer；`background_review_memory` metadata 门禁阻止删除后调用 `knowledge`/plan 工具。所有失败均 fail-open，进程退出可能放弃未完成的一波，侧边会话/子代理不包含在内。上面的 continuity 保存最后一条 AI 回复供下一次对话；本抢救写入持久 memory 行。

## ♻️ TaskFlow 自动恢复

活动 flow 会被重新浮现到系统提示词中，使新会话能接手未完成的工作。三个独立的读取者使用同一套配方——会话级 `get_active_flows_sync(session_id)`，经由 `PromptDataProvider.get_active_flows(session_id)` 获取：

| 读取者 | 位置 | 用途 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | 系统提示词中的 `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | 压缩摘要提示词中的 TaskFlow 区块 |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:215` | 持久化连续性状态中的 `taskflow_ids` |

`creator_session_key` 在创建 flow 时被写入（`taskflow_create.py:38`），值为 `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"`（`_shared.py:21`）。`get_active_flows_sync()`（`store_sqlite.py:466`）仅返回 `running` 与 `waiting` 的 flow，按版本排序，使用无需事件循环的 stdlib `sqlite3` 路径；失败时返回 `[]`。

系统提示词区块（`prompt_builder.py:140`）形如：

```
## Pending TaskFlows
- [running] flow-1: "<描述>" | 2/5 steps done | next: step-3 "<任务>"
Use taskflow_inspect(action="summary") to inspect a flow and continue execution.
```

它最多三个 flow，并且在以文件过滤方式构建提示词（`selected_file_names is not None`）时被抑制。每次读取都失败开放。

