# 长程任务完整能力差距分析 — Long-Running Task Gap Analysis

> 配套文件: SESSION_MEMORY_BORROWING_PLAN.md (22 项记忆层方案)
> 日期: 2026-09-11

## 现状总结

sherry-agent 已有的长程任务基础设施：

| 能力                | 现有实现                                                                                                                                         | 评估              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| 任务状态机          | TaskFlow (5态, 乐观锁, steps/results, waiting/resume)                                                                                            | ★★★★ 状态管理完备 |
| 子代理持久会话      | SpawnMode.SESSION + subagent_registry.db                                                                                                         | ★★★★ 完备         |
| 子代理崩溃恢复      | orphan recovery (3次重试, 24h wedged)                                                                                                            | ★★★★ 完备         |
| 子代理超时          | run_timeout_seconds + wall-clock deadline                                                                                                        | ★★★ 已有单次超时  |
| Swarm 并行          | COLLECT/DISTRIBUTE, max_concurrent                                                                                                               | ★★★ 并行调度有    |
| 完成通知            | push-based 自动通知父会话                                                                                                                        | ★★★★ 完备         |
| 结构化输出验证      | validate_structured_output                                                                                                                       | ★★ 仅 schema 验证 |
| 压缩管线            | T1-T5 五触发 + 多策略 + 恢复上下文                                                                                                               | ★★★★★ 最完备      |
| LangGraph 检查点    | ThreadSafeAsyncSqliteSaver                                                                                                                       | ★★★ 跨重启存活    |
| HITL 审批           | approval pipeline + 永久 allowlist                                                                                                               | ★★★ 单点审批      |
| 跨会话记忆          | MEMORY.md/USER.md + FTS5 搜索                                                                                                                    | ★★ 基础           |
| **任务依赖图 DAG**  | `taskflow_run_task` 支持 `depends_on`; StepStatus 4态 (blocked→ready→dispatched→done); `deps_satisfied()`/`unlock_dependents()`; resume 自动解锁 | ★★★★ **已实现**   |
| **并行步骤执行**    | `taskflow_dispatch` 批量派发 + `taskflow_wait_all` 等待                                                                                          | ★★★★ **已实现**   |
| **Step 级状态追踪** | steps 内 `status` 字段 (blocked/ready/dispatched/done); `taskflow_summary` 展示各步状态与计数; `taskflow_dispatch` 可重派 ready 步骤             | ★★★ **部分实现**  |

---

## 差距清单（记忆层 22 项之外的 12 项）

### 1. 任务依赖图（DAG） — ✅ 已实现

**现状**：~~`taskflow_run_task` 每次 append 一个 step 到 `state["steps"]` 列表，steps 之间无依赖关系。~~

**已完成**：

- `taskflow_run_task` 支持 `depends_on: list[str]` 参数声明前置步骤
- `StepStatus` 枚举: `BLOCKED` → `READY` → `DISPATCHED` → `DONE` (`config.py`)
- `deps_satisfied()`: 检查所有 `depends_on` 步骤是否 `done` (`_shared.py`)
- `unlock_dependents()`: resume 时自动将满足依赖的 blocked 步骤解锁为 ready (`_shared.py`)
- `mark_step_done()`: 按 `child_session_key` 标记步骤完成 (`_shared.py`)
- `taskflow_summary` 展示 `depends_on` 和每步状态
- `taskflow_dispatch` 支持 blocked-but-satisfied 步骤的批量派发
- 测试覆盖: `test_step_graph.py` (单元), `test_run_task_dag.py`, `test_resume_dag.py`, `test_summary_dag.py`, `test_dag_e2e.py` (E2E 含模拟重启)

**实现细节**:

- DAG 字段全部存储在 `state_json` 内, 无 DB schema 迁移
- 未知 `depends_on` id 在 spawn 前拒绝
- 自依赖永不满足 (防循环)
- `unlock_dependents` 单次扫描, 依赖环不会死循环
- legacy 步骤 (无 `status` 字段) 向后兼容: 有 `child_session_key` → dispatched, 否则 → ready

---

### 2. 并行步骤执行 — ✅ 已实现

**现状**：~~`taskflow_run_task` 一次 dispatch 一个子代理, TaskFlow 层面无法同时 dispatch 多个步骤并等待全部完成。~~

**已完成**：

- `taskflow_dispatch` 工具: 批量派发多个 ready/blocked-but-satisfied 步骤
  - `step_ids: list[str]` 参数, all-or-nothing 校验 (任何无效 id 拒绝整批, 零 spawn)
  - 中途 spawn 失败: 已成功 spawn 的步骤在单次 `update_flow` 中持久化, 不丢失
  - flow-level `child_session_key` 不被此工具修改 (per-step key 为准)
- `taskflow_wait_all` 工具: flow-scoped bounded-poll 等待所有 dispatched 步骤完成
  - 仅轮询当前 flow 的 dispatched 步骤的 child_session_key, 不阻塞无关后台子代理
  - 可注入 registry seam (`get_run_by_child_session_key` / `is_live_unended_run`), 测试可替换
  - 超时返回部分报告; 未知 run 视为已 settled (不挂起)
- 测试覆盖: `test_taskflow_dispatch.py` (7+ 场景), `test_taskflow_wait_all.py` (6 场景)
- `build_taskflow_tools()` 返回 10 个工具 (含 dispatch + wait_all)

> 注: `taskflow/__init__.py` 的 `__all__` 尚未导出 `taskflow_dispatch`/`taskflow_wait_all` (仅 `tools/__init__.py` 导出), 不影响功能但建议补齐。

---

### 3. 每个 TaskFlow 的 Token/Cost 预算跟踪 — ❌ 未实现

**现状**：`mes_memory.db` 的 messages 表记录了每条消息的 `input_tokens`/`output_tokens`/`model_name`，但没有按 TaskFlow 聚合。无法知道"这个任务花了多少 token/费用"。

**长程任务影响**：长程任务可能消耗大量 token，无预算预警会失控。

**参考**：hermes-agent 的 `session_model_usage` 表按会话+模型聚合使用量。opencode-dev 的 `session` 表有 `cost` 和 `tokens_*` 字段。

**需要做**：

- `task_flows` 表新增 `total_tokens INTEGER DEFAULT 0` 和 `total_cost REAL DEFAULT 0` 列
- 子代理完成时将 token 用量汇总到关联的 TaskFlow
- 新增 `taskflow_budget` 工具：设置和查询任务预算
- 超预算时触发告警或自动暂停

---

### 4. 任务级截止时间（Deadline） — ❌ 未实现

**现状**：子代理有 `run_timeout_seconds`（单次执行超时），但 TaskFlow 没有整体截止时间。一个跨多会话的任务可以无限期运行。

**长程任务影响**：无截止时间的任务可能永远不完成，占用资源。

**参考**：openclaw 的 `SessionContextBudgetStatus` 有 token 预算管理。hermes-agent 无任务级截止时间。

**需要做**：

- `task_flows` 表新增 `deadline_ts REAL` 列
- `taskflow_create` 支持 `deadline_hours: float` 参数
- Sweeper（已有子代理清扫器）扩展：扫描超截止时间的 TaskFlow，标记为 failed
- 赅时通知用户

---

### 5. 检查点级任务恢复 — 🟡 部分实现

**现状**：子代理 orphan recovery 是从子代理会话级别恢复（重新 steer 消息），但 TaskFlow 层面没有"从第 N 步恢复"的概念。如果 TaskFlow 有 10 个步骤，完成到第 7 步时系统崩溃，恢复后需要模型重新读取 `taskflow_summary` 确认进度——靠模型理解而非系统强制。

**已完成**：

- ✅ `state["steps"]` 的每个 step 已有 `status` 字段 (`blocked`/`ready`/`dispatched`/`done`) — 即差距原述的 `step_status` 字段
- ✅ `taskflow_summary` 展示每步状态 + 按状态计数 (`step_statuses` 行)
- ✅ `taskflow_dispatch` 可批量重派 ready 步骤 (崩溃后模型读 summary → dispatch ready 步骤)
- ✅ `taskflow_resume` 的幂等 `result_hash` 防重复注入
- ✅ E2E 测试覆盖模拟重启场景 (`test_dag_e2e.py`: fresh store init + new event loop → wait_all → resume → finish)

**仍缺失**：

- ❌ 无专用 `taskflow_resume_from` 工具 (自动跳过 done 步骤, 重派 failed/dispatched 步骤)
- ❌ 无 `failed`/`skipped` step 状态 (代码注释: "failure-aware transitions are deliberately deferred to a later phase")
- 无 会话启动时自动调用恢复逻辑 (依赖 LT-2, 同样未实现)

**剩余工时**: 约 1-1.5 天 (step 状态模型已就绪, 需实现 `taskflow_resume_from` + 联动 LT-2)

---

### 6. 进度报告工具 — ❌ 未实现

**现状**：`taskflow_summary` 返回原始的 steps/results 列表（现已含 step 状态计数）。没有面向用户的进度报告（"已完成 60%，预计还需 2 小时"）。

**长程任务影响**：用户在长程任务中无法快速了解进度，必须阅读原始步骤列表。

**参考**：openclaw 无专用进度报告。hermes-agent 的 `session_search` BROWSE 模式可列出会话但非任务进度。

**需要做**：

- 新增 `taskflow_progress` 工具：返回结构化进度报告
  - 总步骤数 / 已完成 / 进行中 / 待处理 / 失败
  - 每个步骤的状态和简短描述
  - 预估剩余时间（基于已完成步骤的平均耗时）
  - 当前等待中的步骤（WAITING 状态）

---

### 7. 步骤结果验证 — ❌ 未实现

**现状**：子代理有 `validate_structured_output`（仅 schema 格式验证），但 TaskFlow 的 `taskflow_resume` 直接接受子代理结果字符串，无内容质量验证。模型靠对话理解判断结果是否可用。

**长程任务影响**：子代理返回错误结果被直接注入 TaskFlow，后续步骤基于错误结果执行，错误级联放大。

**参考**：openclaw 无结果验证。hermes-agent 无。

**需要做**：

- `taskflow_run_task` 支持 `validation_criteria: str` 参数（自然语言描述验收标准）
- `taskflow_resume` 前调用验证：
  - 用便宜模型检查结果是否满足 `validation_criteria`
  - 不满足 → 标记步骤为 `needs_retry`，重新 dispatch 或上报
- 或：用 LLM 生成结构化验收函数（如 `{"must_contain": "PASS", "must_not_contain": "ERROR"}`）

---

### 8. 步骤级重试策略 — ❌ 未实现

**现状**：子代理 orphan recovery 有 3 次重试，但这是子代理级别的。TaskFlow 步骤失败后没有策略——要么模型自己决定重试，要么跳过。无声明式重试策略。

**长程任务影响**：可重试的瞬时失败（网络超时、API 限流）和不可重试的根本错误（代码 bug）无法区分，要么全部重试浪费 token，要么全部放弃。

**参考**：无项目有此能力。

**需要做**：

- `taskflow_run_task` 支持 `retry_policy: dict` 参数
  - `max_retries: int`（默认 0，不重试）
  - `retry_delay_seconds: float`（默认 60）
  - `retry_on: list[str]`（哪些错误类型触发重试，如 `["timeout", "rate_limit"]`）
- `taskflow_resume` 检测到子代理失败时，根据 retry_policy 自动重新 dispatch
- 超过 max_retries → 标记步骤为 `failed`，通知父会话

---

### 9. 跨会话任务看板 — ❌ 未实现

**现状**：TaskFlow 按 channel+chat 隔离。用户在频道 A 启动的任务，在频道 B 看不到。

**长程任务影响**：用户可能在多个频道有长程任务，无全局视图。

**参考**：openclaw 无跨会话看板。hermes-agent 的 `session_search` BROWSE 模式可列会话但非任务。

**需要做**：

- 新增 `taskflow_list` 工具：列出所有活跃 TaskFlow（可选 channel 过滤）
  - 返回：flow_id, status, description, progress (done/total), updated_at
  - 支持按状态过滤（running, waiting, all）
- 这与 LT-2（自动续接）互补：自动续接是被动注入，看板是主动查询

---

### 10. 任务模板/Playbook — ❌ 未实现

**现状**：无任务模板。每个 TaskFlow 从零创建，模型每次重新规划步骤。Skills 系统有 `templates/` 目录但仅用于代码模板，非任务流程模板。

**长程任务影响**：常见工作流（如"部署并验证"、"代码审查并修复"）每次重新规划，质量和一致性不稳定。

**参考**：无项目有此能力。但 sherry-agent 的 Skills 系统是天然的模板载体。

**需要做**：

- 在 Skills 的 `SKILL.md` 中支持 `taskflow_template` frontmatter
  ```yaml
  taskflow_template:
    description: "部署并验证"
    steps:
      - task: "构建项目"
      - task: "部署到测试环境"
        depends_on: [1]
      - task: "运行冒烟测试"
        depends_on: [2]
      - task: "验证结果"
        depends_on: [3]
  ```
- `taskflow_create` 支持 `from_skill: str` 参数，从 Skill 模板创建 TaskFlow
- 模板步骤可带参数占位符（`{project_name}` 等）

---

### 11. 空闲任务检测 — WAITING 太久告警 — ❌ 未实现

**现状**：TaskFlow 有 WAITING 状态（`taskflow_set_waiting`），但无超时检测。一个 WAITING 了 3 天的任务不会被标记异常。

**长程任务影响**：任务卡在等待子代理结果，子代理崩溃后任务永远 WAITING。

**参考**：openclaw 的 sweeper 清扫孤儿子代理。hermes-agent 的压缩失败冷却 600s。

**需要做**：

- `task_flows` 表新增 `waiting_since_ts REAL` 列
- `taskflow_set_waiting` 时写入 `waiting_since_ts`
- `taskflow_resume` 时清除 `waiting_since_ts`
- 子代理 Sweeper（已有）扩展：扫描 WAITING 超过阈值的 TaskFlow
  - 检查关联子代理是否仍活跃
  - 不活跃 → 自动 resume 为失败结果或重新 dispatch
  - 告警通知用户

---

### 12. 任务历史归档与学习 — "上次类似任务怎么做的" — ❌ 未实现

**现状**：TaskFlow 终态（DONE/FAILED/CANCELLED）后留在 `taskflow_registry.db`，但无查询接口（除了 `taskflow_summary` 单个查询）。无法搜索"上次类似的任务"。

**长程任务影响**：长期积累的任务经验无法被检索复用。

**参考**：hermes-agent 的 `session_search` 工具搜索历史会话。sherry-agent 的 `message_search` 工具搜索消息。但都不是 TaskFlow 级别的搜索。

**需要做**：

- 新增 `taskflow_search` 工具：搜索历史 TaskFlow
  - 按描述关键词搜索（FTS5 或子串匹配）
  - 按状态过滤（done, failed, all）
  - 返回：flow_id, description, status, steps 摘要, summary
- DONE 状态的 TaskFlow 的 `summary` 字段作为学习材料
- 与 LT-4（TaskFlow 完成知识提取）联动：搜索历史任务时同时返回提取的知识

---

## 差距汇总

| 编号 | 差距                | 状态        | 优先级 | 类别     | 预估工时    | 与记忆层方案的关系 |
| ---- | ------------------- | ----------- | ------ | -------- | ----------- | ------------------ |
| 1    | 任务依赖图 DAG      | ✅ 已实现   | —      | 执行编排 | (已完成)    | 独立               |
| 2    | 并行步骤执行        | ✅ 已实现   | —      | 执行编排 | (已完成)    | 依赖 #1            |
| 3    | Token/Cost 预算跟踪 | ❌ 未实现   | P0     | 资源管理 | 1天         | 独立               |
| 4    | 任务级截止时间      | ❌ 未实现   | P0     | 资源管理 | 1天         | 独立               |
| 5    | 检查点级任务恢复    | 🟡 部分实现 | P1     | 错误恢复 | 剩余1-1.5天 | 与 LT-2 联动       |
| 6    | 进度报告工具        | ❌ 未实现   | P0     | 用户体验 | 0.5天       | 与 LT-2 联动       |
| 7    | 步骤结果验证        | ❌ 未实现   | P1     | 质量保障 | 1-2天       | 独立               |
| 8    | 步骤级重试策略      | ❌ 未实现   | P1     | 错误恢复 | 1-2天       | 独立               |
| 9    | 跨会话任务看板      | ❌ 未实现   | P1     | 用户体验 | 1天         | 与 LT-2 互补       |
| 10   | 任务模板/Playbook   | ❌ 未实现   | P2     | 执行编排 | 2-3天       | 与 Skills 系统联动 |
| 11   | 空闲任务检测        | ❌ 未实现   | P0     | 错误恢复 | 1天         | 与 Sweeper 联动    |
| 12   | 任务历史归档与学习  | ❌ 未实现   | P2     | 知识管理 | 1-2天       | 与 LT-4 联动       |

---

## 完整实施路线图

```
第一层：记忆层（SESSION_MEMORY_BORROWING_PLAN.md 22 项）
  P0: P0-1~4 + LT-1,2,7,8         (7项, 6-9天)
  P1: P1-1~5 + LT-3,4,5,6         (9项, 12-17天)
  P2: P2-1~5                      (5项, 16-21天)

第二层：执行编排层（本文 12 项）
  ✅ 已完成: #1 DAG + #2 并行步骤                         (2项, 已完成)
  🟡 部分完成: #5 检查点恢复 (step状态模型已就绪)          (剩余1-1.5天)
  P0: #3预算 + #4截止时间 + #6进度报告 + #11空闲检测       (4项, 3.5天)
  P1: #5剩余 + #7结果验证 + #8重试策略 + #9看板            (4项, 5-7天)
  P2: #10模板 + #12历史归档                               (2项, 3-5天)

总计: 22(记忆) + 12(编排) = 34 项
已完成: 2 + 0.5(部分) = 2.5 项
剩余: 31.5 项
预估剩余工时: 42-56 天（约 2 个月）
```

### 优先实施顺序（按价值/成本比排序）

```
1. LT-1  分层记忆存储          (2天)   — 记忆容量基础
2. LT-2  TaskFlow自动续接      (1天)   — 跨会话续接核心
3. #6    进度报告工具          (0.5天) — 最低成本最高用户价值
4. #3    Token/Cost预算跟踪   (1天)   — 成本可控基础
5. #4    任务级截止时间        (1天)   — 防止无限运行
6. #11   空闲任务检测          (1天)   — 防止卡死
7. P0-1  预压缩Memory Flush    (1-2天) — 压缩不丢信息
8. LT-8  会话间意图连续性      (1-2天) — "继续上次"场景
9. LT-7  摘要与TaskFlow协调    (0.5天) — 摘要准确性
10. P0-4 工具输出一行摘要      (0.5天) — 压缩质量
11. #5    检查点恢复 (剩余)    (1-1.5天) — 崩溃自动恢复
...后续按优先级推进
```

> **结论**：实现完整长程任务能力共需 34 项改动（22 记忆 + 12 编排），约 2 个月。
> 已完成 2.5 项（#1 DAG + #2 并行步骤 + #5 检查点恢复-数据模型部分）。
> 只需前 10 项（约 10-12 天）即可覆盖 80% 的长程任务场景：
>
> - 记忆不丢（分层 + Flush + 连续性）
> - 跨会话续接（TaskFlow 自动续接 + 进度报告）
> - 成本可控（预算跟踪 + 截止时间）
> - 不卡死（空闲检测）
> - 摘要准确（TaskFlow 协调 + 工具摘要）
