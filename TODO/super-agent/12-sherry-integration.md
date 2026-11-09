# 12 Sherry 集成:main session 持久化、worker 子代理、MCP 工具

> **修订说明**:本册经推演重写。原版的"worker 无父独立 spawn + hooks 直调"方案不成立——Sherry 的 `spawn_subagent_direct` 要求 `requester_session_key`(父),且续跑 latch 靠**子代理完成通知**重置。正确架构:workers 是 superagent main session 的子代理;`orchestrator_*` 工具经 MCP 暴露;Sherry 挂了 superagent 进 degraded 而非"worker spawn 失败"。

## 1. 集成总览(修正后)

```
superagent 进程                              Sherry 后端进程
┌────────────────────┐                    ┌─────────────────────────┐
│ Engine + Sources   │                    │ superagent main Session  │ ← 持久 Sherry main agent
│ Effects + Outbox   │                    │   (router role, 受限工具) │
│                    │  HTTP loopback     │   ├─ orchestrator_* MCP 工具│
│ SherryExecutor ────┼──→ /internal/ ────┤   ├─ spawn worker subagent │
│   launch effect    │    superagent/*    │   ├─ completion drain 回流 │
│   ↓ inject         │                    │   └─ 续跑 latch 靠完成重置  │
│   candidate via    │                    │                          │
│   auto-turn trigger│←── trajectory/ ────┤ worker subagent (executor)│
│                    │    completion      │   ├─ Sherry terminal/taskflow│
│                    │                    │   ├─ MCP orchestrator_read│
│                    │                    │   └─ 完成 → drain 回 main │
└────────────────────┘                    └─────────────────────────┘
```

**核心修正**:

1. **superagent 拥有一个持久的 Sherry main agent session**(router role)。它不是孤儿——它是 superagent 在 Sherry 里的"驻Sherry代表"。
2. **workers 是该 main session 的子代理**(`spawn_subagent_direct(requester_session_key=<main>)`)。完成 drain 回 main session,**这本身重置 main session 的续跑 latch**——这是 L7 能跑长的关键机制。
3. **`orchestrator_*` 工具经 MCP 暴露**,Sherry 以 MCP 工具加载,不侵入 Sherry 工具注册表。
4. **Sherry 挂了 → superagent 进 degraded**(只收 Trigger 入队,不 dispatch);恢复后重连 main session + 重放 pending Effects。

**接缝原则不变**:superagent 不 import `agent/`;Sherry 不 import `superagent/`;通信走 HTTP loopback。

## 2. 两层接缝分清(修正问题 6)

| 层 | 位置 | 职责 |
|---|---|---|
| **进程内 callable 查找** | `runtime/hooks.py`(Sherry 侧) | Sherry 内部组件解耦用(agent/skills 解析 server-owned callable)。**superagent 不直接用这层**——superagent 是另一个进程。 |
| **HTTP loopback 端点** | `server/trigger/http/superagent.py`(Sherry 侧新增) | Sherry 暴露给 superagent 进程调的 HTTP 路由:`/internal/superagent/*`。这才是 superagent 的接缝。 |
| **MCP server** | `superagent/mcp/server.py`(superagent 侧) | 把 `orchestrator_*` 工具暴露成 MCP,Sherry main session + worker subagent 以 MCP 工具加载。 |

superagent 的 `SherryExecutor` 用 **aiohttp HTTP client** 调 Sherry loopback 路由;**不**拿 `runtime/hooks.py` 的 callable。

## 3. Sherry main session(持久 router)

**这是 superagent 在 Sherry 里的唯一驻点**。一个持久 Sherry main agent session,router role,受限工具子集 + MCP orchestrator 工具。

### 3.1 职责

- 收 superagent 注入的 candidate/follow-up/report 消息(经 auto-turn trigger)。
- turn 跑 routing 决策,调 `orchestrator_route`/`orchestrator_eval` 等 MCP 工具。
- accept → Engine 创建 Ticket → SherryExecutor 让 main session `spawn_subagent_direct` 起 worker。
- worker 完成 → completion drain 回 main session → main session turn 跑 → 调 `orchestrator_report` 报 Engine。
- **worker 完成事件重置 main session 的续跑 latch**(Sherry 既定行为,见 `agent/middlewares/README.md:361`)。

### 3.2 持久性与续跑

- main session 是普通 Sherry main agent session,checkpoint latest-only,WAL 持久化。
- Sherry 续跑 cap(15)对它生效,但每次 worker 完成(=子代理完成通知)重置 latch——所以只要持续有 worker 完成,main session 一直活跃。
- 无 worker 完成 + 无 Trigger → main session 进 idle(续跑 latch 静默),等下一个 Trigger(auto-turn trigger 注入)唤醒。
- **这取代了原计划"worker 跑满报 waiting → Ticket idle"的孤儿模型**:现在是 main session 的 idle 由 Trigger 唤醒,worker 是一次性的。

### 3.3 创建与恢复

- superagent 启动时,经 HTTP `/internal/superagent/session/ensure` 让 Sherry 建(或复用)`superagent-main` session。
- Sherry 重启后,superagent ping `/internal/superagent/session/exists`——在则复用,不在则重建(checkpoint latest-only,丢失的是 in-flight turn,不影响 Ticket 持久状态)。
- superagent 进程重启不影响 main session(它在 Sherry 进程里)。

## 4. Worker↔Sherry subagent(修正问题 1、3)

### 4.1 映射(MVP 选 A:每次 wake 一个新 subagent)

| superagent | Sherry subagent |
|---|---|
| Assignment 的一次 wake | 一个 `spawn_subagent_direct` run |
| Assignment(整个) | 多个 subagent run 的序列(同一 Ticket,跨多个 Trigger 唤醒) |
| worker preset | functional_role=executor + tool 集(MCP + Sherry WORK_TOOLS) |
| shell job 5s 转后台 | Sherry terminal bg |
| nested subagent | Sherry subagent(嵌套,role containment) |
| turn event | trajectory_events + completion drain |
| worker 完成重置 main latch | Sherry 既定行为(wake_on_descendant_settle) |

### 4.2 为什么选 A(每次新 subagent)而非 B(持久 worker session)

- **A 用 Sherry 现有机制**:每个 subagent run 是 goal-loop-bounded 一次性,完成 drain。不需要新 Sherry 代码。
- **B 需要新机制**:持久 worker session 跨多消息注入(turn 间保持活跃、background job 完成唤醒续 turn)——这是 Sherry 没有的能力,要改 turn_runner + state_port + checkpoint 复用。工程量大且破坏 Sherry 现有 subagent 语义。
- **代价**:A 失去"Assignment 内 background job 完成唤醒 executor 续跑"——subagent 完成就 drain,job 结果只能由下一个 wake 的 subagent 读。这符合原型"Executor reads killed-job results on its next turn"。
- **决策**:MVP 选 A。`06-executor.md` 的 Assignment 模型按"一个 Assignment = N 个 subagent run(N = Trigger 唤醒次数)"重写。P2 评估 B 是否值得。

### 4.3 spawn 调用(修正)

```python
# superagent/executor/sherry.py
async def _spawn_worker(self, ticket, assignment_id):
    req = SpawnRequest(
        requester_session_key="superagent-main",   # ← 父是 superagent main session
        functional_role="executor",
        step_model=self.settings.executor_model,
        run_timeout_seconds=_work_budget(ticket),
        cwd=ticket.execution.directory,
        env=self._worker_env(ticket),
        # tools 不在这里传——Sherry subagent 工具来自 registry + MCP
        # orchestrator_* 工具经 MCP server 加载(见 §5)
        response_schema=None,
        expects_completion_message=True,            # ← drain 回 main session
        announce=True,
    )
    result = await self.http.post(
        f"{self.sherry_url}/internal/superagent/spawn",
        json=req.dict(),
    )
    # result.run_id 存 Ticket execution
```

`requester_session_key="superagent-main"` 是关键修正——不再是 None。worker 的 completion drain 回 `superagent-main`,既触发 main session 的下一 turn(routing/report),又重置它的续跑 latch。

### 4.4 Token attribution

AGENTS.md:token 归属到 root parent 的 current turn。superagent main session 是 root(无更上层),worker 的 model 调用归属 main session 的 turn。要确认 `state_register` 对 root main session 的 `TOKEN_BUDGET_PARENT` 正确(应该是 main session 自己)。spike 验证(见 §8)。

## 5. `orchestrator_*` 工具经 MCP 暴露(修正问题 2)

### 5.1 为什么 MCP

Sherry 工具经 `agent/tools/__init__.py::build_main_tools()` 返回 `list[BaseTool]`——进程级注册表,无"spawn 时传外部 callable"入口。要让 main session + worker 调到 superagent 的 `orchestrator_route`/`orchestrator_eval`/`orchestrator_read` 等工具:

- **(A) MCP**(选):superagent 起 MCP server,Sherry 以 MCP 工具加载。不动 Sherry 工具注册表,不动 `build_main_tools()`。
- (B) 改 `build_main_tools()` 条件加载:侵入 Sherry 核心,违背"Sherry 不动"。
- (C) prompt 协议替代:失去工具校验。

### 5.2 MCP server 设计(`superagent/mcp/server.py`)

```python
# superagent/mcp/server.py
# 用 mcp Python SDK(或自写 JSON-RPC over stdio/SSE)
class SuperAgentMCPServer:
    """暴露 orchestrator_* 工具给 Sherry main session + worker subagent。
    Sherry 经 MCP client 连接(配置在 sherry.jsonc 的 mcp.servers)。"""
    tools = [
        orchestrator_read,        # main + worker
        orchestrator_route,       # main only
        orchestrator_eval,        # main only
        orchestrator_eval_read,   # main only
        orchestrator_eval_decide, # main only
        orchestrator_ticket_retry,# main only
        orchestrator_ticket_request_status,
        orchestrator_ticket_note,
        orchestrator_trigger_requeue,
        orchestrator_reply,
        orchestrator_thread_unsubscribe,
        orchestrator_settings_save,
        orchestrator_policy_sync,
        orchestrator_prompts_save,
        # worker-only:
        orchestrator_ack,
        orchestrator_report,
        orchestrator_thread_unrelated,
        orchestrator_thread_follow,
        orchestrator_trigger_handback,
    ]
```

**传输**:stdio(superagent 进程作为 Sherry MCP 子进程)或 SSE(HTTP)。MVP 选 stdio(Sherry 已支持 stdio MCP);Sherry 配置 `mcp.servers.superagent = {command: "python", args: ["-m","superagent.mcp"]}`。

**鉴权**:MCP 工具调 superagent 的 Engine(同进程函数调用),无 HTTP。worker 的 `orchestrator_*` 调用经 MCP → superagent Engine → 按 ticket_id 鉴权(worker 只能操作自己 Ticket,Engine 层 force)。

**角色裁剪**:main session 加载全部 14 个;worker 加载 6 个 worker 工具 + `orchestrator_read`。裁剪在 MCP server 的 tool list 层做(按调用方 session 角色返回不同 tool 集),或在 Sherry preset 层配。MVP 简化:全暴露,Engine 层鉴权拒绝越权(如 worker 调 `orchestrator_route` → Engine 拒)。

### 5.3 工具实现

每个 `orchestrator_*` 工具是 `superagent/app/tools/*.py` 里的函数,调 Engine 对应方法。MCP server 包装成 MCP tool schema。工具描述(prompt 给模型看的)从 `prompts/` 文本读。

## 6. auto-turn trigger 接缝

superagent 的 `Engine.evaluate_due` 决定唤醒 main session:

- 决定要投 candidate/follow-up/report → 经 HTTP `/internal/superagent/session/inject` 向 main session 注消息 → Sherry auto-turn trigger 投递 → main session turn 跑。
- relay/notice effect 同样经 inject 端点。
- main session 的 turn 完成后,Sherry trajectory_events 回调 superagent(`/internal/superagent/events`,反向——superagent 起 HTTP server 收)。

**注意**:这是 Sherry → superagent 的反向调用。superagent 进程也要起一个 loopback HTTP server 收 Sherry 的事件回调(或用 MCP 的 notification 机制)。MVP 用 HTTP loopback 双向。

## 7. state_port 接缝

- `sessions_messages` tool(MCP)后端调 Sherry `/internal/superagent/session/messages` → `state_port.read_messages`。
- restart 时 heal worker 的 interrupted turn:`/internal/superagent/session/heal` → `state_port.heal_interrupted_turn`。
- truncate(retry 时):`/internal/superagent/session/truncate` → `truncate_messages_after_turn`。

## 8. HITL 重定向(不变,承重问题 1 修正后更顺)

worker subagent 内的 HITL 在无人模式 fail-closed(Sherry 既定 headless 行为):

- `question` decline → worker 报 `blocked` + Attention(问题文本)→ MCP `orchestrator_report` → Engine → main session forward attention → GitHub/Feishu 消息。
- 人答 GitHub/Feishu → Source Trigger → 唤醒 → main session turn → Engine resume Ticket → 新 worker subagent(选 A)起 → 读 Thread context(含人答案)→ 续跑。
- `exit_plan_mode` error → worker 报 `waiting`/`dropped`。
- sherry-self-file fail closed → worker 不能改 Sherry/superagent 代码。

per-ticket 准入已授权执行;HITL 只在"需人判断"时触发,转 attention 流。**修正后**:worker 是 main session 子代理,question decline 的"无人 operator"判定成立(main session 的 operator 是 superagent,不是人——headless)。

## 9. 长时无人续跑(修正后的核心价值)

**对比**:

- Sherry 单独:main agent 跑满 15 轮 latch 静默,必须真人消息解锁。
- Sherry + superagent:superagent main session 也受 15 轮 cap,但**每次 worker subagent 完成都重置 latch**。一个 Ticket 的工作 = N 个 worker subagent run(每个 goal-bounded),每个完成 drain 回 main session 重置 latch + 触发下一 routing turn。Ticket 跨多 session run,由 Trigger 事件驱动延续。

**事件源**:GitHub CI/review/comment、Feishu 消息、superagent Passes、worker completion drain。

**跨天**:Ticket 持久 SQLite;main session checkpoint 持久;重启后各自恢复。

**关键**:这套依赖 worker 完成回流重置 latch——**只有 workers 是 main session 子代理才成立**。这是承重问题 1 修正的根本理由。

## 10. 旁进程部署与失败模式(修正问题 7)

### 10.1 进程拓扑(修正)

```
┌─────────────────────────┐      ┌──────────────────────────┐
│  Sherry 后端进程          │      │  superagent 进程           │
│  (Robyn)                 │      │  (Robyn/ASGI, 独立端口)    │
│                          │      │                            │
│  superagent-main session │◄─────┤  Engine + Sources + Effects│
│  (持久 main agent)       │ HTTP │  SherryExecutor            │
│  worker subagents        │ 双向 │  MCP server (stdio→Sherry) │
│                          │      │  HTTP server (收 Sherry 事件)│
│  /internal/superagent/*  │      │                            │
│  SQLite: sherry.db       │      │  SQLite: orchestrator.db   │
└─────────────────────────┘      └──────────────────────────┘
```

双向 HTTP:superagent → Sherry(spawn/inject/heal);Sherry → superagent(trajectory/completion 回调)。MCP 走 stdio(superagent 作为 Sherry MCP 子进程)或 SSE。

### 10.2 失败模式(修正)

| 失败 | 影响 | 恢复 |
|---|---|---|
| superagent 挂了 | Sherry 正常(用户用 Sherry 原生功能);main session idle 等注入;面板不可达 | superagent 重启 → 重连 main session → 重放 pending Effects |
| Sherry 挂了 | **superagent 进 degraded**:只收 Trigger 入队、不 dispatch worker、不跑 main session turn;Ticket 卡在 working/idle | Sherry 恢复 → superagent ping main session → 复用或重建 → 重放 pending Effects(Effects 持久,main session checkpoint latest-only) |
| 都挂了 | 各自重启恢复 | superagent Effects 重放;Sherry checkpoint 重放;main session 可能丢 in-flight turn(可接受) |
| MCP server 挂了 | main session/worker 调不到 orchestrator 工具 → turn 报工具错误 | superagent 进程级 MCP server 随进程重启 |

**关键修正**:Sherry 挂了 ≠ "worker spawn 失败"。Sherry 挂了 = superagent main session 没了 = superagent 整体停摆(不能路由/spawn/跑 turn)。degraded 模式 = 只入队 Trigger,等 Sherry 恢复。这与原型"processing paused, Sources 仍记 Trigger"语义一致。

### 10.3 配置

- `superagent.jsonc`:`stateDir`/`sherry_backend_url`/`port`/`shared_secret`/`mcp_transport`(stdio|sse)。
- Sherry `sherry.jsonc` 加 `superagent` 段(enabled/url/port/secret)+ `mcp.servers.superagent`(command/args)。

## 11. Sherry 侧最小改动清单(修正)

| 文件 | 改动 | 说明 |
|---|---|---|
| `server/trigger/http/superagent.py` | **新增**:loopback 路由 `/internal/superagent/*`(spawn/inject/heal/messages/truncate/exists/ensure/events 回调) | 这是 superagent 进程的接缝,不是 hooks.py |
| `server/__main__.py` | 注册上述路由 + 启动时 ping superagent(可选) | |
| `agent/tools/subagent/spawn/core.py` | 不改(spawn_subagent_direct 已存在,superagent 经 HTTP 调) | |
| `server/service/turn_runner.py` | 不改(cancel 经现有 session 管理) | |
| `agent/state_port.py` | 不改(read_messages/truncate/heal 已存在,经 HTTP 包装暴露) | |
| `agent/trajectory_events.py` | 不改(读 trajectory 经 HTTP 包装;completion drain 是 subagent 既有) | |
| `runtime/hooks.py` | **不改**(superagent 不用这层,见 §2) | |
| `sherry.jsonc` | 加 `superagent` 段 + `mcp.servers.superagent` | |
| `client/app/pages/superagent.vue` | 新页面(MVP 可选) | |
| `pyproject.toml` | 加 `superagent` 包 + 依赖 | |
| `scripts/superagent` | service 脚本 | |
| `wiki/super-agent/` | 四语言 README | |

**Sherry 核心代码(agent/core.py、middleware、tools、spawn core、turn_runner、state_port、trajectory_events)零改动**——只加 HTTP 包装层 + 配置。这是依赖反转的关键。

## 12. spike(开工前必做,2-3 天)

P2 开工前先验证两个承重假设:

### spike 1:completion drain 重置 main session 续跑 latch

- 在 Sherry 起一个 main agent session。
- spawn 一个 subagent(短任务,如"echo hello")。
- 让 main session 跑满接近 15 轮续跑(注入 busy-work directives)。
- subagent 完成 → 观察 main session 的 latch 是否重置(下一 turn 能否正常跑)。
- **预期**:重置(AGENTS.md 既定行为)。若不重置,整个 L7 模型要重新设计。

### spike 2:MCP 工具被 subagent 调到

- 起一个最小 MCP server(python,stdio,一个 echo tool)。
- Sherry 配 `mcp.servers.test`。
- spawn 一个 subagent,让它调该 MCP 工具。
- **预期**:调到,返回结果。若调不到,MCP 路径要换(评估 SSE 或改 build_main_tools)。

### spike 3:HTTP loopback 双向

- Sherry 起 `/internal/test` 路由;外部进程调。
- Sherry turn 完成后回调外部 HTTP。
- **预期**:双向通。

三个 spike 通过,P2 才有底。失败则架构返工。

## 13. 该册验收门(修正)

- `12-sherry-integration.md` 的架构与 §1 图一致:workers 是 main session 子代理(非孤儿)。
- MCP server 暴露 `orchestrator_*` 全工具;main + worker 经 MCP 调到。
- HTTP loopback 双向:superagent→Sherry(spawn/inject/heal);Sherry→superagent(trajectory/completion)。
- `runtime/hooks.py` 不改(确认 superagent 不用这层)。
- Sherry 核心代码零改动(只加 `server/trigger/http/superagent.py` + 配置)。
- HITL fail-closed → blocked + attention 流(修正后 worker 是 main 子代理,headless 成立)。
- 长时无人:worker 完成重置 main latch(单测模拟 + spike 验证)。
- 失败模式:superagent 挂 Sherry 正常;Sherry 挂 superagent degraded(入队不 dispatch)。
- spike 1/2/3 全通过(开工前)。
- `tests/superagent/test_sherry_integration.py`:mock HTTP + MCP,覆盖 spawn/drain/inject/heal/degraded 全路径。
