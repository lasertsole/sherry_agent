# 会话级内存架构（SESSION 计划）

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

SESSION 内存计划的全部 13 项能力（借鉴自 opencode-dev / oh-my-openagent / hermes-agent / openclaw）均已实现，并包含长程任务编排。设计规则：所有能力都扩展既有基础设施——会话连续性、状态寄存器、MesMemory 迁移——绝不另建平行存储。

> 状态（2026-09-13）：计划已退役，本 README 即权威参考。

## 已实现能力

| 能力 | 位置 |
|---|---|
| 预压缩 Memory Flush | `agent/middlewares/summarization/memory_flush.py` |
| 压缩冷却跨重启持久化 | `agent/middlewares/summarization/core.py` 的 `_COOLDOWN_PERSIST_KEYS` |
| SQLite 压缩锁（TTL、fail-open） | `agent/middlewares/summarization/compaction_lock.py`，迁移 v10 |
| 工具输出一行摘要 | `pub/func/message/tool_output_prune.py` |
| 压缩检查点 + 回溯 | 迁移 v15，`restore_compaction_checkpoint` |
| 消息幂等持久化 | 迁移 v11（`idempotency_key` + 部分唯一索引） |
| `context_eligible` 历史投影 | 迁移 v12；检索默认过滤不合格行 |
| 消息树 + 零拷贝 fork | 迁移 v14（`parent_message_id`、`session_leafs`） |
| 只增事件日志 + 投影器 | 迁移 v16，`context_engine/events/` |
| Context Epoch 快照 | 迁移 v17（`context_epoch` 表），`ContextEpoch` |
| steer/queue 双投递 | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| 向量语义搜索 | 迁移 v13，`context_engine/embeddings/`，`message_search --semantic` |
| TaskFlow 编排 | `docs/long-running-tasks/` |

## 记忆文件

三个单文件记忆存储由 `MemoryStore.load_from_disk()`（`agent/tools/memory.py`）加载，并捕获为冻结的系统提示快照。FACTS.md 在首次使用时从 `workspace/template/<lang>/FACTS.md` 懒创建；压缩前 memory flush 不写该文件。

| 文件 | 用途 | 上限 | 维护者 |
|---|---|---|---|
| `workspace/memory/MEMORY.md` | Agent 侧笔记：环境事实、项目约定、工具怪癖 | 2200 字符（`MEMORY_TOOL["memory_char_limit"]`） | `memory` 工具（`memory` 目标：memory review nudge、memory flush） |
| `workspace/memory/USER.md` | 用户画像：偏好、沟通风格、期望 | 1375 字符（`MEMORY_TOOL["user_char_limit"]`） | `memory` 工具（`user` 目标：memory review nudge、memory flush） |
| `workspace/memory/FACTS.md` | 跨计划普适的坑与约定；每次系统提示都会注入 | 1375 字符（`MEMORY_TOOL["facts_char_limit"]`），超限滚动淘汰最旧条目 | `memory` 工具（`facts` 目标：memory review / plan extraction nudge） |

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

- **`context_engine/events/`** —— 只增事件日志（会话内无间隙序列：`types.py`、`store.py`），`EventProjector` 将检查点事件映射到检查点读模型。
- **`context_engine/embeddings/`** —— 向量语义搜索：惰性嵌入后端（项目嵌入模型，可覆盖）、幂等 LEFT-JOIN 索引器、余弦排序；由 `message_search` 工具暴露（`semantic: true`）。
- **`agent/tools/message_search.py`** —— 两段式检索：先在已持久化的 `messages` 表上做 FTS5 搜索；无命中时降级到会话的最新 checkpoint（`SRC_DIR/checkpoints/sqlite.db` 的 `state["messages"]`），由新到旧对尚未持久化的轮次做关键词匹配（受 `_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars` 限制）；兜底命中标记 `source="checkpoint"`。由于持久化发生在每个模型边界与每次工具返回，这条兜底仅在"检查点领先于落库"的极端窗口内起作用 —— 即等待下一个模型边界落库的 HITL 拒绝消息（工具结果在返回时即已写入）。
- **`agent/middlewares/message_persistence/`** —— 写一次的会话持久化，两个时机：human/AI 消息在每个模型调用边界，工具结果在返回瞬间；`persisted_message_ids` 水位保证每条消息恰好落库一次，持久化不依赖压缩触发。
- **上下文体积治理** —— `ContextEvictionMiddleware` 在超大工具结果（> 20 000 字符）进入 state 之前将其卸载到 `SESSIONS_DIR/<session_id>/evicted/`（state 中只留 head+tail 预览）；超长人类消息（> 200 000 字符）写入同一目录并打上 `lc_evicted_to` 标记，但只截断模型视图——state 与 MesMemory 保留全文。`clear_session()` 删除整个会话目录，驱逐文件一并清除。P1-2 溢出尾部裁剪只经 `model_copy` 把尾部 `ToolMessage` 内容替换为 stub（身份与配对不变）—— 不丢数据，因为每条结果都已落库、被卸载的原文也留在磁盘上：`message_search` 可取回文本，`read_file` 可重读驱逐文件。
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
