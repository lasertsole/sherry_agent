# 12 Sherry 集成：Ticket↔TaskFlow、Executor↔subagent、hooks 接缝

这是 Python 版 super-agent 与原型最大的差异，也是把它从"复刻品"变成"Sherry 的 L7 指挥层"的关键。原型依赖 DeepSeek Harness 的 Session API；Python 版用 Sherry 本体的 subagent + TaskFlow + hooks。

## 1. 集成总览

```
superagent 进程                              Sherry 后端进程
┌────────────────────┐                    ┌─────────────────────┐
│ Engine + Sources   │                    │ agent.core          │
│ SherryExecutor ────┼─── runtime/hooks ──┤ spawn_subagent_direct│
│   launch effect    │    (HTTP loopback) │ turn_runner.cancel  │
│   ↓                │                    │ state_port          │
│   spawn_subagent() │                    │ trajectory_events   │
│   ↓                │                    │                     │
│   Ticket = flow    │                    │ TaskFlow DAG        │
│   Executor = run   │                    │ Evidence ledger     │
└────────────────────┘                    └─────────────────────┘
```

**接缝原则**：

- superagent **不 import** `agent/`（lint-imports 强制，见 `01-architecture.md §4`）。
- superagent 经 `runtime/hooks.py` 注册的回调调 Sherry。
- Sherry **不 import** `superagent/`；只在启动时注册 hooks（若 superagent 在跑）。
- 通信走 HTTP loopback（`aiohttp` client），不阻塞事件循环。
- 两进程独立重启周期。

## 2. runtime/hooks.py 扩展

Sherry 现有 `runtime/hooks.py` 是进程级回调注册表（AGENTS.md："`agent`/`skills` resolve server-owned callables without importing `server`"）。新增 superagent 注册点：

```python
# runtime/hooks.py（Sherry 侧扩展）
class SuperAgentHooks:
    """superagent 进程注册的回调。Sherry 启动时若检测到 superagent
    进程（HTTP ping），注册这些；否则空实现（superagent 不在跑时
    Sherry 正常工作）。"""
    spawn_subagent: Callable[[SpawnRequest], SpawnResult]
    cancel_turn: Callable[[str], None]                 # session_key
    add_messages: Callable[[str, list[Message]], None] # session_key, msgs
    truncate_after: Callable[[str, int], None]         # session_key, turn_ordinal
    heal_interrupted: Callable[[str], int]             # session_key → healed_calls
    read_session_state: Callable[[str], SessionState]
    list_jobs: Callable[[str], list[Job]]
    kill_job: Callable[[str, str], None]               # session_key, job_id
    read_trajectory: Callable[[str], Trajectory]
```

Sherry 后端实现这些（委托给 `agent/tools/subagent/spawn/core.py`、`server/service/turn_runner.py`、`agent/state_port.py`、`agent/trajectory_events.py`），暴露成 HTTP loopback 端点（`POST /internal/superagent/*`，仅 loopback + shared-secret）。

superagent 的 `SherryExecutor` 通过 `SuperAgentHooks` client 调这些，不直接 import Sherry 代码。

## 3. Ticket ↔ TaskFlow flow 映射

**映射规则**：

| superagent | Sherry TaskFlow |
|---|---|
| Ticket | 一个 flow（`taskflow_flow(action="create")`） |
| Ticket 的 steps | flow 的 steps（Executor 自己拆，或 accept 时由 main Session 拆） |
| Assignment | flow 的一个执行波次 |
| Report `ready` | `taskflow_flow(action="finish")` 触发 gate |
| Report `failed`/`blocked` | step 标 failed/blocked |
| Ticket end | flow 终止（finish/cancel） |
| Evidence ledger | 复用 Sherry `agent/tools/pub_base/sqlite_store.py` 的 evidence ledger |
| 回归门 | 复用 Sherry finish gate A-C + E |

**为什么不强制 1:1**：

- 不是每个 Ticket 都需要 TaskFlow DAG（简单 bug fix 一步到位）。Executor（Sherry subagent）自己决定是否开 flow。
- Ticket 是 superagent 的授权/编排单位；flow 是 Sherry 的执行编排单位。Ticket 的 `execution.directory` + `mandate.scope` 是 flow 的上下文。
- 默认映射：Ticket accept 时 main Session 可选 `taskflow_flow(action="create")` 开一个 flow，flow_id 记在 Ticket `execution` 字段；Executor 每个 Assignment 在该 flow 下跑 steps。

**finish gate 复用**：

- Sherry 的 finish gate（DAG 完整性 + 无未决 step + evidence 无 FAIL/stale/broken + goal judge）作为 Ticket `ready` Report 的前置。
- 即：Executor 报 `ready` 前，Sherry 的 finish gate 必须先过。这把 Sherry 的本地静态验证 + superagent 的外部交付验证 合流。
- 实现：`SherryExecutor.report` handler 在转 `Engine.report` 前，若 `kind=ready`，先调 Sherry `taskflow_flow(action="finish")` 的 gate 结果；gate 不过 → 转 `blocked` Report（带 gate 反馈）。

## 4. Executor Session ↔ Sherry subagent run

**映射规则**：

| superagent | Sherry subagent |
|---|---|
| Executor Session（`worker-<ticket_id>`） | 一个 subagent run（`spawn_subagent_direct`） |
| Session preset（worker/router） | functional_role + tool 集裁剪 |
| turn | subagent 的一个执行轮 |
| shell job（5s 转后台） | Sherry terminal 工具的 background job |
| nested subagent | Sherry subagent（嵌套） |
| inbox（消息注入） | `state_port.add_messages` |
| turn event | `trajectory_events` + completion callback |
| Session restart | subagent checkpoint 恢复（Sherry latest-only） |

**spawn 调用**：

```python
# superagent/executor/sherry.py
async def _spawn_worker(self, ticket, assignment_id):
    req = SpawnRequest(
        session_key=self.worker_session_id(ticket.id),
        parent_session=None,                  # 独立（非主 agent 子）
        functional_role="executor",
        step_model=self.settings.executor_model,
        run_timeout_seconds=_work_budget(ticket),
        cwd=ticket.execution.directory,
        env=self._worker_env(ticket),
        system_prompt=self.prompts.compose(role="worker", ticket=ticket),
        tools=_worker_tools(),                # 受限集
        response_schema=None,
        announce=True,                        # 完成时通知（superagent 收）
    )
    result = self.hooks.spawn_subagent(req)
    # result.run_id 存 Ticket execution
```

**关键差异处理**：

1. **Sherry subagent 的 continuation 上限**：Sherry 有 `max_continuation_turns=15`。一个 worker subagent 跑满 15 轮会 latch 静默。**superagent 不绕过它**——而是让 worker 报 `progress`（自动续跑 cap 内），cap 到了 worker 报 `waiting`（idle），Ticket 进 idle stage，等下一个 Trigger 唤醒（CI 结果/review/人消息）新 Assignment 续跑。这是 L7 的正确模型：不无限续跑一个 session，而是事件驱动多 session。

2. **Sherry HITL 在 worker 内**：worker subagent 的 HITL（question/exit_plan_mode/sherry-self-file）在无人模式 fail-closed（AGENTS.md 已有 headless 行为）。superagent 的 per-ticket 准入已授权，worker 内 HITL 拒绝时 worker 报 `blocked`（带问题）→ main Session forward `attention` → 人答 → resume。这是把 Sherry 的 HITL 升级为 superagent 的 Attention 机制。

3. **subagent completion judge**：Sherry 的 CompletionJudge（goal loop）在 worker 完成时跑。superagent 的 `SherryExecutor` 收 completion → 转 `Engine.report`（kind 由 completion verdict 决定：pass→ready、retry→progress、block→blocked）。

## 5. main Session ↔ Sherry 主 agent 或独立 subagent

**选项 A（推荐 MVP）**：main Session 是 Sherry 主 agent 的一个特殊 mode，用 `dsh-superagent-router` preset（受限工具子集 + noWait 守卫）。superagent 经 hooks 向主 agent 注入 candidate/follow-up/report 消息。

**选项 B**：main Session 是独立 subagent（router role），与 worker 同等但工具集不同。隔离更好但多一层 RPC。

**选 A 的理由**：main Session 主要是 routing（轻 LLM 调用），与 Sherry 主 agent 共享 provider 配置/cache。MVP 走 A，P2 可迁 B。

## 6. auto-turn trigger 接缝

Sherry 的 auto-turn trigger（AGENTS.md：PlanContinuationEnforcer 经 auto-turn trigger 投递）是 superagent 唤醒 idle Ticket 的关键路径：

- superagent 的 `Engine.evaluate_due` 决定要唤醒 idle Ticket → 经 hooks 向 main Session 注入 candidate 消息 → Sherry auto-turn trigger 投递 → main Session turn 跑（routing 决策）。
- superagent 的 `relay`/`notice` effect 也经 auto-turn trigger 投递 main Session。
- `runtime/effects/` 二层（Sherry 重启韧性）可选启用：superagent 的三个 restart-sensitive delivery（plan continuation / subagent completion / auto-turn）经 Sherry effects 二层保活。但 superagent 自己有 Effects 持久队列 + restart replay，所以**不依赖** Sherry effects 二层——superagent 进程重启后自己的 Effects 重放。

## 7. state_port 接缝

`agent/state_port.py`（AGENTS.md：read live message list / pending HITL / heal interrupted turn）用于：

- `SherryExecutor` 读 worker Session 的 live transcript（`sessions_messages` 等价）—— superagent 的 `sessions_messages` tool 后端调 `state_port.read_messages`。
- restart 时 heal worker Session 的 interrupted turn（dangling tool call）—— `heal_interrupted` 调 `state_port.heal_interrupted_turn`。
- truncate：retry/restart 时 `truncate_messages_after_turn` 清掉旧 turn。

## 8. HITL 重定向

worker subagent 内的 HITL：

- `question` 工具：无人模式 decline（Sherry 已有 `NO_OPERATOR_MESSAGE`）。worker 收 decline → 报 `blocked` + Attention（问题文本）→ main Session forward → 人答 → Trigger 唤醒 worker resume。
- `exit_plan_mode`：无人模式 error（"stop"）。worker 收 error → 报 `waiting` 或 `dropped`。
- sherry-self-file 审批：无人模式 fail closed。worker 不能改 Sherry 自身代码（superagent 也不该）。
- `interrupt_on` 通用策略：无人模式 deny。

这些 fail-closed 行为**正是 superagent 想要的**——worker 不挂起等人，而是把"需要人"转成 Report + Attention。superagent 的 per-ticket 准入已授权执行，HITL 只在"需要人判断"时触发，转成 attention 流。

## 9. 长时无人续跑（核心价值）

**对比 Sherry 单独**：

- Sherry 单独：`max_continuation_turns=15` 后 latch 静默，必须真人消息解锁。
- Sherry + superagent：一个 worker subagent 跑满 15 轮报 `waiting` → Ticket idle → **下一个 Trigger（CI/review/人消息/cron）唤醒新 Assignment** → 新 worker subagent 续跑。Ticket 跨越多 session，由事件驱动延续，不受单 session 续跑 cap。

**事件源**：

- GitHub CI 结果（PR 推后 CI 跑完 → Trigger）。
- GitHub review（人 review → Trigger）。
- GitHub comment（人评论 → Trigger）。
- Feishu 消息。
- superagent 自己的 Passes（每 2 秒 Eval due）。
- Sherry auto-turn trigger（main Session 的 routing turn）。

**跨天目标延续**：Ticket 持久化在 SQLite，重启后重放。一个跨三天的 Ticket：今天跑到 idle 关掉，明天 CI 跑完 Trigger 唤醒，后天人 review Trigger 再唤醒。这是 L7 的"长时无人值守"。

## 10. 旁进程部署细节

### 10.1 进程启停

- `scripts/superagent start` 起 superagent 进程（tmux 会话 `superagent`，与 Sherry 后端独立）。
- 进程入口 `superagent/__main__.py`：建 Runtime + 注册 Source plugins + start Passes/Effects。
- Sherry 后端启动时（`server/__main__.py`）ping superagent loopback（`GET /state`）—— 在跑则注册 `SuperAgentHooks` client；不在跑则空实现。
- superagent 启动时 ping Sherry 后端 loopback —— 在跑则 `SherryExecutor` 用真 hooks；不在跑则退化（worker 起不来，fail Ticket）。

### 10.2 配置

- `superagent.jsonc`（在 Sherry home 或独立）：
  - `stateDir`：superagent 数据目录。
  - `sherry_backend_url`：Sherry loopback（默认 `http://127.0.0.1:8080`）。
  - `port`：superagent 面板/API 端口（默认 3082，与原型一致）。
  - `shared_secret`：internal API 鉴权（loopback 仍同源守卫，secret 双保险）。
- Sherry `sherry.jsonc` 加 `superagent` 段（enabled/url/port/secret）。

### 10.3 失败模式

- superagent 挂了：Sherry 后端继续正常工作（hooks 空实现）；面板 `/superagent` 页显示不可达。
- Sherry 挂了：superagent 的 worker spawn 失败 → Report `failed`（transient，重试 3 次）→ Ticket `person/failed` 等人。
- 都挂了：重启后各自恢复（superagent 重放 Effects；Sherry 重放 checkpoint）。

## 11. Sherry 侧最小改动清单

| 文件 | 改动 |
|---|---|
| `runtime/hooks.py` | 加 `SuperAgentHooks` 注册点 + HTTP client |
| `server/__main__.py` | 启动时 ping superagent，注册 hooks |
| `server/trigger/http/` | 加 `/internal/superagent/*` loopback 路由（spawn/cancel/add_messages/truncate/heal/read_state/list_jobs/kill_job/read_trajectory） |
| `agent/tools/subagent/spawn/core.py` | 暴露 `spawn_subagent_direct` 给 hooks（已存在，加 HTTP 包装） |
| `server/service/turn_runner.py` | 暴露 cancel/heal 给 hooks |
| `agent/state_port.py` | 暴露 read_messages/truncate/heal 给 hooks（已存在） |
| `agent/trajectory_events.py` | 暴露 read_trajectory 给 hooks |
| `client/app/pages/superagent.vue` | 新页面（MVP 可选） |
| `sherry.jsonc` | 加 `superagent` 段 |
| `pyproject.toml` | 加 `superagent` 包 + 依赖（pydantic/aiohttp） |
| `scripts/superagent` | service 脚本 |
| `wiki/super-agent/` | 四语言 README |

**Sherry 核心代码（agent/core.py、middleware、tools）不动**——只暴露 hooks。这是依赖反转的关键。

## 12. 该册验收门

- `runtime/hooks.py` 的 `SuperAgentHooks` 注册点定义 + 空 + HTTP client 实现。
- Sherry `/internal/superagent/*` loopback 路由：spawn/cancel/add_messages/truncate/heal/read_state/list_jobs/kill_job/read_trajectory 全实现 + loopback + shared-secret 守卫。
- `SherryExecutor` 全部经 hooks 调 Sherry，不 import `agent/`（lint-imports 强制）。
- Ticket↔flow 映射：accept 可开 flow；ready 前 finish gate 过；blocked 时转 gate 反馈。
- Executor↔subagent：spawn/cancel/add_messages/jobs 全路径单测（mock hooks）。
- HITL 重定向：worker question/exit_plan_mode/sherry-self-file 在无人模式 fail-closed → Report blocked/Attention。
- 长时无人：worker 跑满 15 轮报 waiting → Ticket idle → Trigger 唤醒新 Assignment（单测模拟 CI Trigger）。
- 旁进程：superagent 挂了 Sherry 正常；Sherry 挂了 superagent worker spawn failed→Ticket person/failed。
- `tests/superagent/test_sherry_integration.py`：mock Sherry HTTP，覆盖 spawn/cancel/relay/heal/restart 全路径。
- Sherry 侧 `tests/server/test_superagent_hooks.py`：hooks 注册 + loopback 守卫 + 各端点。
- 端到端（`tests/superagent/test_e2e.py`，可标 `system`/`llm_e2e`）：真实 GitHub 测试仓库 + Sherry 后端 + superagent 进程，issue→Ticket→PR→CI→修→merge 全链路。
