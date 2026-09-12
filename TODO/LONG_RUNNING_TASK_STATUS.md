# Sherry 长程任务能力完成度总览

> 日期: 2026-09-11
> 配套文件: TODO/SESSION_MEMORY_BORROWING_PLAN.md (22 项) · TODO/LONG_RUNNING_TASK_GAP_ANALYSIS.md (12 项)

---

## 1. 自主规划与动态调度机制

| 子能力                     | 状态      | 现有实现                                                                                                                                                                        | 差距                |
| -------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 任务拆解                   | ✅ 已实现 | TaskFlow 5态状态机 (running/waiting/done/failed/cancelled), 乐观锁, SQLite持久化 (`taskflow_registry.db`)                                                                       | —                   |
| 子代理调度                 | ✅ 已实现 | `taskflow_run_task` dispatch 子代理, Swarm COLLECT/DISTRIBUTE 并行                                                                                                              | —                   |
| 流式纠错-LLM重试           | ✅ 已实现 | `llm_retry.py`: 错误分类→可重试/不可重试→backoff重试→fallback链→stale-streak断路器                                                                                              | —                   |
| 流式纠错-循环检测          | ✅ 已实现 | `tool_guardrails.py`: 5种病态检测 (exact_failure/same_tool_failure/no_progress/ping_pong/arg_churn) + recovery mode                                                             | —                   |
| 流式纠错-崩溃循环          | ✅ 已实现 | `crash_loop_breaker.py`: 3次unclean boot in 5min → trip                                                                                                                         | —                   |
| 迭代预算                   | ✅ 已实现 | `iteration_budget.py`: 每 agent 上限 50 次迭代, 耗尽返回 terminal message                                                                                                       | —                   |
| 任务依赖图 DAG             | ✅ 已实现 | `taskflow_run_task` 支持 `depends_on`; `StepStatus` 4态 (blocked→ready→dispatched→done); `deps_satisfied()`/`unlock_dependents()`; `taskflow_resume` 自动解锁; E2E+单元测试覆盖 | —                   |
| 并行步骤执行               | ✅ 已实现 | `taskflow_dispatch` 批量 dispatch (all-or-nothing 校验); `taskflow_wait_all` flow-scoped bounded-poll 等待; 中途失败持久化已成功 spawn                                          | —                   |
| **步骤级重试策略**         | ❌ 缺失   | 无声明式 `retry_policy` (max_retries/retry_delay/retry_on)                                                                                                                      | 差距 #8, P1, 1-2天  |
| **任务模板/Playbook**      | ❌ 缺失   | 无可复用工作流模板, Skill 未承载 taskflow_template                                                                                                                              | 差距 #10, P2, 2-3天 |
| **跨会话TaskFlow自动续接** | ✅ 已实现 | 新会话不自动扫描未完成 TaskFlow                                                                                                                                                 | LT-2, P0, 1天       |

**完成度: 9/11 (82%)** — 核心调度、DAG 依赖、并行步骤和纠错能力完备, 但重试策略和模板缺失。

---

## 2. 高效的上下文工程与外部记忆机制

| 子能力                 | 状态      | 现有实现                                                                                 | 差距            |
| ---------------------- | --------- | ---------------------------------------------------------------------------------------- | --------------- |
| 状态外置存储           | ✅ 已实现 | MesMemory: SQLite (`mes_memory.db`), WAL模式, 消息持久化                                 | —               |
| FTS5全文搜索           | ✅ 已实现 | dual FTS5 (unicode61 + trigram), CJK路由, LIKE fallback, 上下文展开                      | —               |
| 压缩管线               | ✅ 已实现 | T1-T5 五触发 + 多策略 (dedup/prune/truncate/LLM摘要) + 溢出恢复 (MAX_OVERFLOW_RETRIES=3) | —               |
| 跨会话记忆             | ✅ 基础   | MEMORY.md (2200字符) + USER.md + `message_search` 工具                                   | —               |
| **预压缩Memory Flush** | ✅ 已实现 | 压缩时关键信息永久丢失                                                                   | P0-1, 1-2天     |
| **压缩失败冷却持久化** | ❌ 缺失   | 冷却仅内存态 (`state_register_mem`), 重启丢失                                            | P0-2, 0.5天     |
| **压缩锁防并发**       | ❌ 缺失   | 跨实例无保护                                                                             | P0-3, 1天       |
| **工具输出摘要替代**   | ✅ 已实现 | 直接删除/截断, 非一行摘要替代                                                            | P0-4, 0.5天     |
| **压缩检查点回溯**     | ❌ 缺失   | 压缩后旧消息不可恢复                                                                     | P1-1, 2-3天     |
| **增量幂等标记**       | ❌ 缺失   | 无 idempotency_key, 崩溃重试可能重复写入                                                 | P1-2, 1天       |
| **活跃上下文投影**     | ❌ 缺失   | 无 `context_eligible` 标记                                                               | P1-3, 1-2天     |
| **Recall自动召回**     | ❌ 缺失   | 需主动调用, 无自动跨会话注入                                                             | P1-4, 2-3天     |
| **转录树/消息分支**    | ❌ 缺失   | 线性列表, fork需全量复制                                                                 | P1-5, 2天       |
| **分层记忆存储**       | ✅ 已实现 | MEMORY.md 硬限2200, 无 facts/ 结构化层                                                   | LT-1, P0, 2天   |
| **摘要链式退化防护**   | ❌ 缺失   | 多次压缩后信息逐层稀释                                                                   | LT-3, P1, 1-2天 |
| **TaskFlow知识提取**   | ❌ 缺失   | 任务完成不触发知识提取                                                                   | LT-4, P1, 1-2天 |
| **子代理记忆回流**     | ❌ 缺失   | 子代理记忆不回父会话                                                                     | LT-5, P1, 1天   |
| **记忆过期清理**       | ❌ 缺失   | 无 TTL/冲突检测/定期整理                                                                 | LT-6, P1, 2天   |
| **摘要与TaskFlow协调** | ✅ 已实现 | 压缩摘要不含 TaskFlow 状态                                                               | LT-7, P0, 0.5天 |
| **会话间意图连续性**   | ✅ 已实现 | 新会话不注入上一会话末尾状态                                                             | LT-8, P0, 1-2天 |
| **事件溯源**           | ❌ 缺失   | 无事件持久化/重放                                                                        | P2-1, 5-7天     |
| **Context Epoch**      | ❌ 缺失   | 无系统上下文快照机制                                                                     | P2-2, 2-3天     |
| **双水位Facts提取**    | ❌ 缺失   | 无自动事实提取                                                                           | P2-3, 3-4天     |
| **steer/queue双投递**  | ❌ 缺失   | 仅 FIFO 队列, 活跃 turn 中不可注入                                                       | P2-4, 3天       |
| **向量嵌入语义搜索**   | ❌ 缺失   | 仅 FTS5 关键词, 无语义匹配                                                               | P2-5, 3-4天     |

**完成度: 9/23 (39%)** — 基础存储和压缩管线完备, 记忆编排层 (LT-1~~8) 核心项已落地, 高级上下文管理 (P0~~P2) 仍待补齐。

---

## 3. 耐久运行时与容灾恢复机制

| 子能力             | 状态        | 现有实现                                                                                                                                                                                      | 差距                     |
| ------------------ | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| Crash Loop Breaker | ✅ 已实现   | 3次unclean boot/5min → trip, clean-exit marker, state文件容错                                                                                                                                 | —                        |
| 子代理崩溃恢复     | ✅ 已实现   | orphan recovery: 3次重试, 24h wedged检测, 自动 re-steer                                                                                                                                       | —                        |
| TaskFlow持久化     | ✅ 已实现   | SQLite WAL, 乐观锁, 跨重启存活                                                                                                                                                                | —                        |
| LangGraph检查点    | ✅ 已实现   | ThreadSafeAsyncSqliteSaver                                                                                                                                                                    | —                        |
| 双层状态管理       | ✅ 已实现   | `state_register_mem` (内存) + `state_register_db` (SQLite)                                                                                                                                    | —                        |
| 幂等resume         | ✅ 已实现   | `taskflow_resume` 的 `result_hash` 幂等检测                                                                                                                                                   | —                        |
| 检查点级任务恢复   | 🟡 部分实现 | Step 级 `status` 字段已实现 (blocked/ready/dispatched/done); `taskflow_summary` 展示各步状态; `taskflow_dispatch` 可重派 ready 步骤; 但无专用 `taskflow_resume_from` 工具, 无会话启动自动恢复 | 差距 #5, P1, 剩余1-1.5天 |
| **任务级截止时间** | ✅ 已实现   | 无 `deadline_ts`, 任务可无限期运行                                                                                                                                                            | 差距 #4, P0, 1天         |
| **空闲任务检测**   | ✅ 已实现   | WAITING 超时无检测, 子代理崩溃后任务永远 WAITING                                                                                                                                              | 差距 #11, P0, 1天        |
| **压缩冷却持久化** | ❌ 缺失     | 冷却仅内存态, 重启后立即重试失败压缩                                                                                                                                                          | P0-2, 0.5天              |

**完成度: 8.5/10 (85%)** — 崩溃恢复基础和 step 级状态追踪完备, 任务级截止时间与空闲检测已落地, 仅剩自动检查点恢复缺失。

---

## 4. 可观测性与安全护栏机制

| 子能力                 | 状态      | 现有实现                                                                                                                                  | 差距                |
| ---------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| HITL审批               | ✅ 已实现 | 完整 pipeline: detection (危险模式) + approval (命令审批) + gates (write/interrupt/MCP/kanban/pairing/slash) + strategies + 永久allowlist | —                   |
| 迭代预算控制           | ✅ 已实现 | `iteration_budget.py`: 每 agent 上限 50 次, 耗尽返回 terminal                                                                             | —                   |
| 工具循环检测           | ✅ 已实现 | `tool_guardrails.py`: 5种病态 + BLOCK/HALT + recovery mode                                                                                | —                   |
| 全链路日志             | ✅ 已实现 | Loguru, 每次工具调用/模型调用/状态变更均有 debug/warning/error 日志                                                                       | —                   |
| 压缩效果跟踪           | ✅ 已实现 | `CompressionEffectivenessTracker` (`summarization_components.py`)                                                                         | —                   |
| 会话级状态隔离         | ✅ 已实现 | `state_register_mem` 按 session_id 隔离, `clear_all_register_sessions`                                                                    | —                   |
| **Token/Cost预算跟踪** | ✅ 已实现 | messages 表有 input_tokens/output_tokens/model_name, 但无按 TaskFlow 聚合                                                                 | 差距 #3, P0, 1天    |
| **进度报告工具**       | ✅ 已实现 | `taskflow_summary` 返回原始列表+step状态计数, 但无预估时间/结构化进度报告                                                                 | 差距 #6, P0, 0.5天  |
| **跨会话任务看板**     | ❌ 缺失   | TaskFlow 按 channel+chat 隔离, 无全局视图                                                                                                 | 差距 #9, P1, 1天    |
| **步骤结果验证**       | ❌ 缺失   | `taskflow_resume` 直接接受结果, 无内容质量验证                                                                                            | 差距 #7, P1, 1-2天  |
| **任务历史归档搜索**   | ❌ 缺失   | 终态 TaskFlow 留在 DB 但无搜索接口                                                                                                        | 差距 #12, P2, 1-2天 |

**完成度: 8/12 (67%)** — 安全护栏和审批链完备, 预算跟踪和进度报告已落地, 但结果验证缺失。

---

## 总结

| 维度                    | 已实现   | 待实现   | 完成度  |
| ----------------------- | -------- | -------- | ------- |
| 1. 自主规划与动态调度   | 9        | 2        | 82%     |
| 2. 上下文工程与外部记忆 | 9        | 14       | 39%     |
| 3. 耐久运行时与容灾恢复 | 8.5      | 1.5      | 85%     |
| 4. 可观测性与安全护栏   | 8        | 4        | 67%     |
| **总计**                | **34.5** | **21.5** | **62%** |

**近期进展 (2026-09-10 → 09-11)**:

- ✅ 差距 #1 任务依赖图 DAG — 已实现, `depends_on` + 4态 StepStatus + 自动解锁
- ✅ 差距 #2 并行步骤执行 — 已实现, `taskflow_dispatch` 批量派发 + `taskflow_wait_all` 等待
- 🟡 差距 #5 检查点级任务恢复 — 部分实现, step 级状态追踪已就绪, 专用恢复工具缺失

**完成里程碑 (2026-09-12)**: 以下 10 项已实现并提交，对应计划文档已从 `TODO/` 移除：

- ✅ LT-1 分层记忆存储 · LT-2 TaskFlow自动续接 · LT-7 摘要与TaskFlow协调 · LT-8 会话间意图连续性
- ✅ P0-1 预压缩Memory Flush · P0-4 工具输出一行摘要
- ✅ #3 Token/Cost预算跟踪 · #4 任务级截止时间 · #6 进度报告工具 · #11 空闲任务检测

**最薄弱环节**: 上下文工程与外部记忆 (39%) — 记忆层方案与编排层差距合计仍有 21.5 项未完成。

**优先实施前10项** (已于 2026-09-12 全部完成):

1. LT-1 分层记忆存储 (2天)
2. LT-2 TaskFlow自动续接 (1天)
3. #6 进度报告工具 (0.5天)
4. #3 Token/Cost预算跟踪 (1天)
5. #4 任务级截止时间 (1天)
6. #11 空闲任务检测 (1天)
7. P0-1 预压缩Memory Flush (1-2天)
8. LT-8 会话间意图连续性 (1-2天)
9. LT-7 摘要与TaskFlow协调 (0.5天)
10. P0-4 工具输出一行摘要 (0.5天)
