# 会话级内存架构（SESSION 计划）

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

SESSION 内存计划的全部 13 项能力（借鉴自 opencode-dev / oh-my-openagent / hermes-agent / openclaw）均已实现，并包含长程任务编排。设计规则：所有能力都扩展既有基础设施——会话连续性、状态寄存器、MesMemory 迁移——绝不另建平行存储。

> 状态（2026-09-13）：计划已退役，本 README 即权威参考。

## 已实现能力

| 项 | 能力 | 位置 |
|---|---|---|
| P0-1 | 预压缩 Memory Flush | `agent/middlewares/summarization/memory_flush.py` |
| P0-2 | 压缩冷却跨重启持久化 | `agent/middlewares/summarization/core.py` 的 `_COOLDOWN_PERSIST_KEYS` |
| P0-3 | SQLite 压缩锁（TTL、fail-open） | `agent/middlewares/summarization/compaction_lock.py`，迁移 v10 |
| P0-4 | 工具输出一行摘要 | `pub/func/message/tool_output_prune.py` |
| P1-1 | 压缩检查点 + 回溯 | 迁移 v15，`restore_compaction_checkpoint` |
| P1-2 | 消息幂等持久化 | 迁移 v11（`idempotency_key` + 部分唯一索引） |
| P1-3 | `context_eligible` 历史投影 | 迁移 v12；检索默认过滤不合格行 |
| P1-5 | 消息树 + 零拷贝 fork | 迁移 v14（`parent_message_id`、`session_leafs`） |
| P2-1 | 只增事件日志 + 投影器 | 迁移 v16，`context_engine/events/` |
| P2-2 | Context Epoch 快照 | 迁移 v17（`context_epoch` 表），`ContextEpoch` |
| P2-4（部分） | steer/queue 双投递 | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| P2-5 | 向量语义搜索 | 迁移 v13，`context_engine/embeddings/`，`message_search --semantic` |
| TaskFlow 编排 | `docs/long-running-tasks/` |

## MesMemory 迁移（v10–v17）

| 版本 | 结构 |
|---|---|
| v10 | `compression_locks` —— 每会话压缩锁（主键即互斥） |
| v11 | `messages.idempotency_key` + 部分唯一索引（崩溃重试去重） |
| v12 | `messages.context_eligible` —— 上下文投影标记（默认 1） |
| v13 | `message_embeddings` —— 语义搜索向量索引 |
| v14 | `messages.parent_message_id` + `session_leafs` —— 消息树 |
| v15 | `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` |
| v16 | `events` —— 只增日志，会话内无间隙 `seq` |
| v17 | `context_epoch` —— 系统上下文基线 / 快照 |

## 关键组件

- **`context_engine/events/`** —— 只增事件日志（会话内无间隙序列：`types.py`、`store.py`），`EventProjector` 将检查点事件映射到 P1-1 读模型。
- **`context_engine/embeddings/`** —— 向量语义搜索：惰性嵌入后端（项目嵌入模型，可覆盖）、幂等 LEFT-JOIN 索引器、余弦排序；由 `message_search` 工具暴露（`semantic: true`）。
- **`agent/tools/message_search.py`** —— 两段式检索：先在已持久化的 `messages` 表上做 FTS5 搜索；无命中时降级到会话的最新 checkpoint（`SRC_DIR/checkpoints/sqlite.db` 的 `state["messages"]`），由新到旧对尚未持久化的轮次做关键词匹配（受 `_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars` 限制）；兜底命中标记 `source="checkpoint"`。由于持久化现在发生在每个模型边界，这条兜底仅在"检查点领先于落库"的极端窗口内起作用 —— 即当轮尚未到下一落库边界的在途工具结果（与 HITL 拒绝）。
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
