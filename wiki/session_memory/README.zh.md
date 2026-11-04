# 会话级内存架构（SESSION 计划）

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

SESSION 内存计划的 13 项能力中，12 项仍在（借鉴自 opencode-dev / oh-my-openagent / hermes-agent / openclaw）——压缩前 memory flush 已退役，由压缩时 memory review 覆盖——另含长程任务编排。设计规则：所有能力都扩展既有基础设施——会话连续性、状态寄存器、MesMemory schema——绝不另建平行存储。

> 状态（2026-09-13）：计划已退役，本 README 即权威参考。

## 已实现能力

| 能力 | 位置 |
|---|---|
| 压缩冷却跨重启持久化 | `agent/middlewares/summarization/core.py` 的 `_COOLDOWN_PERSIST_KEYS` |
| SQLite 压缩锁（TTL、fail-open） | `agent/middlewares/summarization/compaction_lock.py`，当前 schema |
| 工具输出一行摘要 | `pub/func/message/tool_output_prune.py` |
| 压缩检查点 + 回溯 | 当前 schema，`restore_compaction_checkpoint` |
| 消息幂等持久化 | 当前 schema（`idempotency_key` + 部分唯一索引） |
| `context_eligible` 历史投影 | 当前 schema；检索默认过滤不合格行 |
| 消息树 + 零拷贝 fork | 当前 schema（`parent_message_id`、`session_leafs`） |
| 只增事件日志 + 投影器 | 当前 schema，`context_engine/events/` |
| Context Epoch 快照 | 当前 schema（`context_epoch` 表），`ContextEpoch` |
| steer/queue 双投递 | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| 向量语义搜索 | 当前 schema，`context_engine/embeddings/`，`message_search --semantic` |
| TaskFlow 编排 | `wiki/long-running-tasks/` |

## 记忆条目

三个长期记忆目标（`memory`、`user`、`facts`）是 MesMemory `memory_entries` 表中的 SQLite 行，每个 `§` 条目一行，通过 `MemoryEntryRepository`（`context_engine/store/memory_entry_repository.py`）读写。旧的 `workspace/memory/{MEMORY,USER,FACTS}.md` 只是遗留种子：`memory_migration.migrate_legacy_memory_files()` 在 `agent_core.init()` 时用单个事务导入一次，提交成功后才把每个文件改名为 `*.md.bak`。只要表里已有任意一行，导入就跳过，因此改名失败也不会重复导入；事务失败则保留原文件，下次启动重试。

`MemoryStore.load_from_store()` 按 `created_at ASC, id ASC`（即旧文件从上到下的顺序）读取活跃行，并把渲染结果冻结为系统提示快照。会话中途的写入会持久化，但不会重新渲染快照，从而保持 provider 前缀缓存稳定；快照会在下次会话启动、压缩后重建、模型/思考切换以及子代理完成回流时重载。不存在 per-project FACTS；遗留的 `<project>/.sherry/memory/FACTS.md` 被忽略。FACTS 仍只记录系统级 / 跨项目的坑，memory review / plan extraction nudge 会执行该路由。

每行记录 `origin_session`、`usage_count` 和 `last_used`。渲染块会在来源已知的条目后追加 `[origin: <sid>, created: <date>, used: <n>x]`。`memory(action="read")` 返回匹配条目并累加使用次数；`facts` 溢出时归档**最不活跃**的条目（先 `COALESCE(last_used, created_at)`，再 `usage_count`），所有删除都是可恢复的软删除。记忆面板使用 `/memory/entries*`、`/memory/settings` 和 `/memory/consolidate`；旧的 `GET/PUT /memory` 仍保持 `§` 分隔格式。

| 目标 | 用途 | 上限 | 维护者 |
|---|---|---|---|
| `memory` | Agent 侧笔记：环境事实、项目约定、工具怪癖 | 2200 字符（`MEMORY_TOOL["memory_char_limit"]`，可在面板覆盖） | `memory` 工具 / memory review nudge |
| `user` | 用户画像：偏好、沟通风格、期望 | 1375 字符（`MEMORY_TOOL["user_char_limit"]`，可在面板覆盖） | `memory` 工具 / memory review nudge |
| `facts` | 系统级 / 跨项目坑，注入每次系统提示 | 1375 字符（`MEMORY_TOOL["facts_char_limit"]`，可在面板覆盖），最不活跃者先滚动 | `memory` 工具 / memory review、plan extraction nudge |

## MesMemory schema

| schema 对象 | 用途 |
|---|---|
| `compression_locks` | 每会话压缩锁（主键即互斥） |
| `messages.idempotency_key` + 部分唯一索引 | 崩溃重试去重 |
| `messages.context_eligible` | 上下文投影标记（默认 1） |
| `message_embeddings` | 语义搜索向量索引 |
| `memory_entries` / `memory_settings` | 长期记忆条目（目标、来源、使用量、归档）+ 面板可编辑上限 |
| `messages.parent_message_id` + `session_leafs` | 消息树 |
| `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` | 压缩恢复点 |
| `events` | 只增日志，会话内无间隙 `seq` |
| `context_epoch` | 系统上下文基线 / 快照 |

`build_schema_v1` 一次创建完整 schema；没有版本化迁移链。

## 关键组件

- **`context_engine/events/`** —— 只增事件日志（会话内无间隙序列：`types.py`、`store.py`），`EventProjector` 将检查点事件映射到检查点读模型。
- **`context_engine/embeddings/`** —— 向量语义搜索：惰性嵌入后端（项目嵌入模型，可覆盖）、幂等 LEFT-JOIN 索引器、余弦排序；由 `message_search` 工具暴露（`semantic: true`）。
- **`agent/tools/message_search.py`** —— 两段式检索：先在已持久化的 `messages` 表上做 FTS5 搜索；无命中时降级到会话的最新 checkpoint（`SRC_DIR/checkpoints/sqlite.db` 的 `state["messages"]`），由新到旧对尚未持久化的轮次做关键词匹配（受 `_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars` 限制）；兜底命中标记 `source="checkpoint"`。由于持久化发生在每个模型边界与每次工具返回，这条兜底仅在"检查点领先于落库"的极端窗口内起作用 —— 即等待下一个模型边界落库的 HITL 拒绝消息（工具结果在返回时即已写入）。
- **`agent/middlewares/message_persistence/`** —— 写一次的会话持久化，两个时机：human/AI 消息在每个模型调用边界，工具结果在返回瞬间；`persisted_message_ids` 水位保证每条消息恰好落库一次，持久化不依赖压缩触发。
- **上下文体积治理** —— `ContextEvictionMiddleware` 在超大工具结果（> 20 000 字符）进入 state 之前将其卸载到 `SESSIONS_DIR/<session_id>/evicted/`（state 中只留 head+tail 预览）；超长人类消息（> 200 000 字符）写入同一目录并打上 `lc_evicted_to` 标记，但只截断模型视图——state 与 MesMemory 保留全文。`clear_session()` 删除整个会话目录，驱逐文件一并清除。P1-2 溢出尾部裁剪只经 `model_copy` 把尾部 `ToolMessage` 内容替换为 stub（身份与配对不变）—— 不丢数据，因为每条结果都已落库、被卸载的原文也留在磁盘上：`message_search` 可取回文本，`read_file` 可重读驱逐文件。
- **终端 stdout 不再经过管道** —— `terminal` 工具把子进程的 stdout 直接交给一个文件（stderr 与过去一样并入其中），事后只做一次判断：小于 `TOOLS_TIMEOUTS["fd_write_threshold_bytes"]`（100 000 字节）时整读临时文件并删除；达到或超过时把文件改名进 `evicted/terminal-<sha256(前 64 KiB)[:16]>.log`，返回的是以字节总量（`stat().st_size`——绝不遍历文件数行数）标注的头尾预览。预览本身也按字节封顶，所以哪怕是无换行的 500 MB 输出也只占几 KB；超时或被取消的命令会删掉自己的临时文件，硬杀残留由 `atomic_write.py` 里同一套 `.sherry-tmp-` / `.log` 约定清扫。
- **`agent/middlewares/summarization/compaction_lock.py`** —— SQLite 压缩锁（TTL 自愈、同步 + 异步获取、超时 fail-open），包裹 `_apply_compression` 与 `_aapply_compression` 两条路径。
- **`runtime/session/state_register.py`** —— `ContextEpoch` 生命周期（initialize / prepare / replace / advance），基于 `context_epoch` 表。

## 测试

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/context_engine/store/test_add_messages_idempotency.py \
    tests/context_engine/store/test_compaction_checkpoints.py \
    tests/context_engine/store/test_message_tree.py \
    tests/context_engine/events/test_events.py \
    tests/runtime/test_context_epoch.py \
    tests/context_engine/embeddings/test_semantic_search.py \
    tests/agent/tools/test_message_search_checkpoint_fallback.py -q
```

## 评估

```bash
uv run python evals/evals.py session_memory
```

在评估沙箱内对活体子系统评分——6 项检查覆盖冷却重启存活、锁互斥、检查点回溯、幂等重放、上下文投影与真实嵌入语义排序。见 `evals/session_memory/suite.py`。
