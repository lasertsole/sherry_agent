# REVISIONS — 计划组修订日志

## 修订 1:推演后承重问题修正(2026-11-09)

对初稿做了一遍推演,核对 Sherry `spawn_subagent_direct` 源码(`agent/tools/subagent/spawn/core.py`)和续跑机制(`agent/middlewares/README.md:361`),发现 8 个问题,其中 2 个承重。已全部修回对应分册。

### 承重问题(已修)

#### P1-Worker 不能无父独立 spawn

- **初稿写**:`spawn_subagent(parent_session=None, ...)` 起独立 worker(`06-executor.md`、`12-sherry-integration.md`)。
- **实际**:Sherry `spawn_subagent_direct` 要求 `requester_session_key`(父)。整个机制(depth/role containment/completion_owner/token attribution/wake_on_descendant_settle)假设有父 main session。无孤儿路径。
- **为什么承重**:L7 模型靠"worker 完成重置 main session 续跑 latch"(AGENTS.md:361:"a subagent completion resets the budget and the latch")。worker 孤儿则完成不回流,latch 永不重置,跑满 15 轮死锁。
- **修正**:workers 改为 superagent main session 的子代理。新增"superagent 持久 main session"概念(router role)。`12-sherry-integration.md` 全文重写;`06-executor.md §1/§2` 改;`01-architecture.md §2` 进程拓扑改。

#### P2-`orchestrator_*` 工具无法注入 Sherry subagent

- **初稿写**:`_spawn_kwargs(tools=_worker_tools())` 传工具集。
- **实际**:Sherry 工具经 `build_main_tools()` 进程级注册表,无"spawn 时传外部 callable"入口。superagent 的 Python 函数工具不在 Sherry 注册表。
- **修正**:superagent 起 MCP server(`superagent/mcp/server.py`)暴露 `orchestrator_*`,Sherry 以 MCP client 加载(`sherry.jsonc` 配 `mcp.servers.superagent`)。不侵入 `build_main_tools()`。`01-architecture.md §5b` 新增;`10-sessions-prompts.md §3-§4` 改;`12-sherry-integration.md §5` 新增。

### 重要问题(已修)

#### P3-Worker Session 跨多消息注入 vs Sherry 一次性 goal loop

- **初稿隐含选 B**(持久 worker session,background job 完成唤醒续 turn)但没说清。
- **实际**:Sherry subagent 是 goal-loop-bounded 一次性,完成 drain。无"持久 session 跨多消息注入"原生支持。
- **决策**:MVP 选 A(每次 Trigger 唤醒起新 subagent run)。P2 评估 B。`06-executor.md §4` 后台作业简化;`12-sherry-integration.md §4.2` 新增决策说明。

#### P4-Store 生成列表达式无效

- **初稿写**:`json_extract(inputs_json, '$[#-1].delivered')` 等 JSON path 生成列。
- **问题**:SQLite JSON path `$[#]` 是最后元素,`$[#-1]`(倒数第二)不支持算术。序列化 shape 变化也会让生成列静默失效。
- **修正**:复杂状态改显式列(`ended_at`/`latest_assignment_status`/`has_undelivered_input`),Engine 写时维护。保留的生成列只做简单 `CASE WHEN`/`OR`。`04-store.md §2` 全 schema 改。

#### P5-Prompt 是重写不是翻译

- **初稿写**:"从原型 prompts/ 移植"。
- **实际**:原型 prompt 引用 Harness 概念(preset/inbox/job_output/Session event),Sherry 工具表面不同(terminal/taskflow/todolist/MCP)。
- **修正**:`10-sessions-prompts.md §10` 新增"Prompt 重写指引":工具名替换、去 Harness 引用、保留语义重写例子。`test_cards.py`/`test_model_surface.py` 断言按 Sherry 表面调整。

#### P6-`runtime/hooks.py` 与 HTTP loopback 层级混淆

- **初稿写**:`runtime/hooks.py` 加 `SuperAgentHooks` + HTTP 端点。
- **实际**:`runtime/hooks.py` 是进程内 callable 查找,不是 HTTP 端点暴露。两件事混了。
- **修正**:分清两层——`runtime/hooks.py` 不改(superagent 不用);`server/trigger/http/superagent.py`(Sherry 新增)是 HTTP 接缝;MCP 是工具暴露。`12-sherry-integration.md §2` 重写;`12-sherry-integration.md §11` Sherry 改动清单改(hooks.py 从清单移除)。

#### P7-Sherry 挂了 ≠ "worker spawn 失败"

- **初稿写**:"Sherry 挂了:worker spawn 失败 → Ticket person/failed"。
- **实际**:workers 是 main session 子代理(P1 修正后),main session 是 Sherry 会话。Sherry 挂 = main session 没了 = superagent 整体停摆。
- **修正**:改 degraded 模式(只入队 Trigger,等 Sherry 恢复)。`12-sherry-integration.md §10` 重写;`15-rollout.md §9` 风险表更新。

### 小问题(已修或在开工时处理)

- **`test_bundle_patch.py`**:Harness 专属(cordis.patch.yml),Python 版无 bundle patch。从 `13-test-plan.md` 删除。
- **`WorkerSlots.release()` 失败路径未绑**:加 `try/finally`。`06-executor.md` 隐含。
- **`MemoryRepositories` 同序**:dict 不保序,改 OrderedDict。`04-store.md §7`。
- **basedpyright strict**:`dict` 字段 untyped access。精确类型或单独 pyright 配置降 strict。
- **Windows prepare**:`/bin/sh -c` → `bash -c`(Git Bash)。`06-executor.md §5`。
- **Token attribution**:P1 修正后 worker 归属 main session root。spike 1 验证。
- **`runtime/effects/` 二层**:superagent 自己有 Effects;Sherry 侧 main session checkpoint 不保。P2 评估。

### 新增:P-Spike 期

- **位置**:`15-rollout.md §1b` 新增,在 P0 之前。
- **内容**:3 个承重假设验证(completion 重置 latch / MCP 工具 / HTTP 双向),3 天。
- **失败处理**:任一不过回到 `12-sherry-integration.md` 重新设计,不盲开 P2。
- **测试**:`13-test-plan.md §5.13` 新增 spike 测试。

### 影响的分册

| 分册 | 改动 |
|---|---|
| `01-architecture.md` | §2 进程拓扑重写(加 main session + MCP);§5b 新增 MCP server |
| `04-store.md` | §2 schema 全改(显式列替代 JSON path 生成列) |
| `06-executor.md` | §1 定位改;§2.1/§2.2 spawn 改(parent=main + 不传 tools);§4 后台作业简化(选 A) |
| `10-sessions-prompts.md` | §1.1/§1.2 改(main session 持久 + worker 子代理);§3/§4 加 MCP;§10 新增 prompt 重写指引 |
| `12-sherry-integration.md` | **全文重写** |
| `13-test-plan.md` | 删 `test_bundle_patch.py`;§5.13 新增 spike 测试 |
| `15-rollout.md` | §1 总览加 P-Spike;§1b 新增 spike 期;§9 风险表更新;§11 开工清单更新 |

### 未改的分册

`00-overview.md`、`02-domain-model.md`、`03-engine.md`、`05-sources.md`、`07-authorization.md`、`08-assessment.md`、`09-effects-outbox.md`、`11-panel-http.md`、`14-non-goals.md` 不受这批修正影响(域/引擎/Source/授权/评估/Effects/面板/非目标与 Sherry 集成无关或仅间接相关)。
