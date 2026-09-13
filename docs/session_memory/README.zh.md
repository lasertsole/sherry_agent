# 会话级内存架构（SESSION 计划）

[English](README.md) · 中文 · [日本語](README.ja.md) · [한국어](README.ko.md)

`TODO/SESSION_MEMORY_BORROWING_PLAN.md`（借鉴 opencode-dev / oh-my-openagent / hermes-agent / openclaw）的实现落地情况。设计规则：所有新能力都扩展既有 long-running-task 基础设施（分层 facts、会话连续性、状态寄存器），绝不另建平行存储。

## 已实现

| 项 | 能力 | 位置 |
|---|---|---|
| P0-1 | 预压缩 Memory Flush | `agent/middlewares/memory_flush.py` |
| P0-2 | 压缩失败冷却跨重启持久化 | `agent/middlewares/summarization.py` 的 `_COOLDOWN_PERSIST_KEYS` |
| P0-3 | SQLite 压缩锁（TTL、fail-open） | `agent/middlewares/compaction_lock.py`，MesMemory 迁移 v10 |
| P0-4 | 工具输出一行摘要 | `pub/func/message/tool_output_prune.py` |
| P1-2 | 消息幂等持久化 | MesMemory 迁移 v11（`idempotency_key` + 部分唯一索引）、`add_messages` 标记 |
| P1-3 | `context_eligible` 历史投影 | MesMemory 迁移 v12；检索默认过滤不合格消息 |
| P2-4（部分） | steer/queue 双投递 | `announce/steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn`；子代理引导走 `sessions_steer` |
| LT-* | TaskFlow DAG / 预算 / 截止 / 重试 / 连续性 | `docs/long-running-tasks/` |

## 待实现路线图

P1-1 压缩检查点回溯 · P1-4 跨会话记忆召回 · P1-5 转录树与消息分支 · P2-1 事件溯源迁移 · P2-2 Context Epoch 快照 · P2-3 双水位游标 Facts 提取（必须落在既有分层 facts 存储上）· P2-5 向量语义搜索。

## 测试

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py tests/agent/middlewares/test_compression_cooldown_persist.py tests/context_engine/store/test_add_messages_idempotency.py -q
```
