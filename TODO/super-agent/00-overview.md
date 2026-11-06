# 00 概览：目标、范围、复刻清单

## 1. 目标

把 DeepSeek super-agent 研究原型（TypeScript，Harness 插件）**完全复刻**为一个 Python 子系统 `superagent/`，驻留在 Sherry 仓库内，并用 Sherry 的 subagent 系统替代原型的 Harness Executor。完成后 Sherry 具备：

- **长时无人值守**：Ticket 状态机 + 事件驱动唤醒，不再受 Sherry 原 `max_continuation_turns=15` 硬上限约束。
- **外部反馈闭环**：GitHub/Feishu Source 把 issue/PR/review/CI 变化翻译成 Trigger，唤醒 idle Ticket。
- **目标跨会话持久化**：Ticket（origin/execution/follows/inputs/assignments）持久化在 SQLite，重启后重放。
- **准入式授权**：HITL 从 per-action 前移到 per-ticket 准入，worker 一旦被接受就全权执行（受限工具集 + blocked commands）。

## 2. 完全复刻清单（逐项打勾）

下表把原型功能拆成可独立验收的项。每一项在后续分册里有对应的设计与测试。

### 2.1 域与引擎（core）

- [ ] 域类型：Source / Trigger / Thread / Correlation / Channel / Policy / Eval / Mandate / Ticket / Assignment / Subscription / Report / Attention / Disposition / Input / Scope / Strength —— 见 `02-domain-model.md`。
- [ ] Engine 五部件：Intake / Evals / Tickets / Assignments / Replies —— 见 `03-engine.md`。
- [ ] Engine 同步事务模型：每个命令一个事务，端口在事务内入队、适配器在 commit 后执行 —— `03-engine.md §事务模型`。
- [ ] 角色接口 Pick：`EngineForSources` / `EngineForMainSession` / `EngineForPeople` / `EngineForSettings` / `EngineForExecutor` —— `03-engine.md §角色接口`。
- [ ] 共享谓词：`waits_for_person` / `followup_owners` / `settled` / `ticket_stage` / `can_retry` —— `03-engine.md §谓词`。
- [ ] 域事件：`trigger.received` / `eval.started` / `decision.recorded` / `ticket.created` / `ticket.resumed` / `ticket.ended` / `assignment.started` / `assignment.ended` / `thread.subscribed` / `thread.unsubscribed` / `trigger.requeued` / `policy.changed` —— `03-engine.md §事件`。
- [ ] 固定拒绝：`UserError(code, values)` + `USER_ERRORS` 字典；HTTP 404=missing、400=其余、409=stale revision —— `03-engine.md §错误`。

### 2.2 持久化（store）

- [ ] 单 SQLite 文件 `<stateDir>/orchestrator.sqlite`，目录锁防第二进程 —— `04-store.md`。
- [ ] 表：`trigger` / `disposition` / `trigger_thread` / `thread` / `ticket` / `decision` / `effect` / `outbox` / `kv` —— `04-store.md §schema`。
- [ ] 生成列 + 索引：`thread.active` / `thread.subscribed` / `ticket.open` / `ticket.active` / `outbox.dropped` —— `04-store.md §生成列`。
- [ ] Repository：`atomic` + `triggers` / `threads` / `tickets` / `decisions`，各 `get/save/list` —— `04-store.md`。
- [ ] 读查询 `read`：`thread_triggers` / `trigger_stats` / `search` / `dropped_replies` 等，永不写 —— `04-store.md §读查询`。
- [ ] KV 类型化键值：`KV` 单一登记点，所有读写走它 —— `04-store.md §KV`。
- [ ] `after_commit`：仅在 commit 后执行，rollback 后不执行 —— `04-store.md`。
- [ ] 迁移：`SCHEMA_VERSION` 单调增；新版本库拒绝旧库启动；schema 10→11 一次性升级 —— `04-store.md §迁移`。
- [ ] 内存版 `MemoryRepositories`：供域测试用，与 Store 同序同嵌套回滚 —— `04-store.md §内存版`。

### 2.3 Sources

- [ ] `SourceKind` 协议：`kind` / `title` / `description` / `schema` / `fields` / `facts` / `health` / 可选 `scope` / `channel` / `thread_context` / `find` / `policy` / `environment` / `refresh_labels` —— `05-sources.md`。
- [ ] `SourceRegistry`：注册 kind、管 instance、claim 健康项、防碰撞 —— `05-sources.md §registry`。
- [ ] `SourceContext` / `SourceHost` 窄接口：`receive` / `atomic` / `state` / `threads` / `tickets` / `notify` / `report` —— `05-sources.md`。
- [ ] GitHub 源：通知轮询 + 订阅 Issue/PR/评论/CI、PR 跟随与关闭、`claim_pull`/`observe_pull`、`pull_end_notices` —— `05-sources.md §GitHub`。
- [ ] Feishu 源：live 事件 + 历史恢复 + 订阅话题轮询、图片读取、chat 词限 —— `05-sources.md §Feishu`。
- [ ] 共享轮询：`POLL_HORIZON_MS` / `polled` / `cursor_at` / `poll_threads`（保留订阅 + 结束后两周窗口）—— `05-sources.md §poll`。
- [ ] Cookbook 配方：`webhook` 示例源作为可加新 Source 的模板与测试 —— `05-sources.md §webhook`。

### 2.4 Executor

- [ ] `DshExecutor`（Python 改名 `SherryExecutor`）：一 Ticket 一 Session 一目录 —— `06-executor.md`。
- [ ] `WorkerSlots`：launch/wake 占槽；`max_workers` + 一额外人消息槽；wake 优先于 launch —— `06-executor.md §slots`。
- [ ] prepare 命令：`sh` 在执行根跑一次，10 分钟超时；失败 fail Ticket —— `06-executor.md §prepare`。
- [ ] 后台作业：5 秒后转后台的 shell job + subagent；turn 结束仍有则 keep slot；30 分钟上限杀 shell、留 subagent —— `06-executor.md §后台`。
- [ ] 并发：并发限制 + 人消息额外槽；CI 等 Input 等正常槽 —— `06-executor.md §并发`。
- [ ] 执行环境变量：`DSH_SUPERAGENT_*` 一套（原型名保留以兼容 prompt 文本）—— `06-executor.md §env`。
- [ ] 失败重试：transient provider/network（HTTP 408/429/5xx、refused/reset/timeout）三次指数退避 —— `06-executor.md §重试`。
- [ ] Restart：interrupted Assignment 带 recovery note 重开；ready/blocked/failed 保持；pending launch 重新排队 —— `06-executor.md §restart`。
- [ ] Pull-request 跟随与结束规则：Ticket 跟随它开的 PR 与起源 PR；所有跟随 PR 关闭才结束；reopen 重跟 —— `06-executor.md §PR`。
- [ ] 自动合并：Delivery requirements prompt 管；默认 <200 行 + 必须审批覆盖 head commit；推新版本前撤 auto-merge —— `06-executor.md §automerge`。

### 2.5 授权与 Policy

- [ ] `Policy`：`version` / `paused` / `eval_timeout_ms` / `gate` / `revalidate` / `admits_inferred` —— `07-authorization.md`。
- [ ] 准入 gate：`addressed`/`direct`、allowed list、bot list、admission 表 —— `07-authorization.md §gate`。
- [ ] 强度：explicit / inferred / not_admitted；三种 case 全覆盖 —— `07-authorization.md §strength`。
- [ ] admission 表：三 cell（listed-unaddressed / unlisted-addressed / unlisted-unaddressed），每 cell `explicit|inferred|ignore` 默认 —— `07-authorization.md §admission`。
- [ ] accept mode：`off` / `observe`(默认) / `auto` —— `07-authorization.md §mode`。
- [ ] Mandate：immutable，记录 Policy version / scope / strength / matched rules / sender / via；revalidation 规则按 basis 分支 —— `07-authorization.md §mandate`。
- [ ] 订阅扇出：bypass gate、不授权；人消息达 Ticket 即归该 Ticket Executor 决定 —— `07-authorization.md §subscription`。
- [ ] handback：Executor 一次 hand back；Ticket 结束释放未读 follow-up —— `07-authorization.md §handback`。
- [ ] context-only 订阅：借上下文不授权不唤醒 —— `07-authorization.md §context-only`。
- [ ] resume：人消息唤醒 ended Ticket（非 stop），revalidation 先行；bot/CI 不唤醒 —— `07-authorization.md §resume`。
- [ ] stop：人 stop 撤 Mandate、Ticket 永久 ended；Eval 决定 stop 强度 —— `07-authorization.md §stop`。
- [ ] Eval 超时：candidate 到达 main session 起计；两次重排后第三次 `unavailable` —— `07-authorization.md §eval-timeout`。
- [ ] Policy 变更：版本变丢弃运行 Eval、重排队列；通知受影响 live Ticket；不立即撤 Mandate —— `07-authorization.md §policy-change`。

### 2.6 评估（MasterAssessor）

- [ ] `MasterAssessor`：`request` / `release` / `context` / `read` / `decide` —— `08-assessment.md`。
- [ ] Eval 上下文：Trigger 文本（≤4000 字符）+ 图片 id + batch 内其他 Trigger；≤8 `related_tickets`（open 优先）+ `tickets_total`；`policy_version`；Thread 窗口（首 5 + 左右各 5，普通 400 字符、左右各 1200）；其他消息 stub 50 字符 —— `08-assessment.md §context`。
- [ ] `orchestrator_eval_read` 按 id 展开 —— `08-assessment.md`。
- [ ] inferred accept 必须 assessment；card 明显无规则命中可直 ignore（`ignoredFromCard`）—— `08-assessment.md §shortcut`。
- [ ] assessment failure → `unavailable`；later Trigger 可开新 Eval —— `08-assessment.md`。

### 2.7 Effects 与 Outbox

- [ ] `Effects` 持久队列：`(kind, key)` 单行；同 key 等待中入队替换 payload 并立即 due；`handle(kind, run, opts)` 每种一个 handler；oldest first、并发限 —— `09-effects-outbox.md`。
- [ ] 六种 effect：`assessment` / `launch` / `delivery` / `relay` / `notice` / `reply`，各并发上限与退避 —— `09-effects-outbox.md §kinds`。
- [ ] `Later` 异常：不计数失败、延后 —— `09-effects-outbox.md`。
- [ ] `Outbox`：单写收据；`send` 在事务内入 `reply` effect；message 等前序同 Ticket 同会话消息；前序失败 3 次才放行 —— `09-effects-outbox.md §outbox`。
- [ ] 投递：`sent` / `gone`（永久丢，保留 dropped）/ throw（重试 5s→300s）—— `09-effects-outbox.md`。
- [ ] `HealthBoard`：每健康项一个 writer、claim 防碰撞；连续 3 次失败才报 error；下次成功自清 —— `09-effects-outbox.md §health`。

### 2.8 Sessions 与 Prompts

- [ ] `Sessions`：`ensure` 用角色 model 建 Session；`install` 跟事件 + 注册 prompts/skills/tools —— `10-sessions-prompts.md`。
- [ ] 主 Session（router）：`dsh-superagent-router` preset；shell/job 5 秒返回；`noWait` 守卫拒 `sleep`、`timeoutMs>30s`、`job_output>5s` —— `10-sessions-prompts.md §main`。
- [ ] worker Session（worker）：`dsh-superagent-worker` preset；shell 5 秒转后台；受限工具集（无 ask/present/profile/scheduling/其他 Session）—— `10-sessions-prompts.md §worker`。
- [ ] `relay` / `notify`：main Session 消息走 `relay`/`notice` effect；turn 中到达的 relay 在下一 step 一起投 —— `10-sessions-prompts.md`。
- [ ] prompt revision：每次 model 请求绑当前 revision；Sessions 持久化用过的 revision；worker 可读自己用过的 —— `10-sessions-prompts.md §revisions`。
- [ ] Prompts：team sections（Communication/Ticket scope/Delivery requirements/Team skills）+ 可覆盖 built-in parts（`runtime/*`、`skill/*`）+ 生成模板（`runtime/environment`、`runtime/policy`、`runtime/policy/none` 占位符）—— `10-sessions-prompts.md §prompts`。
- [ ] 版本：每次 save 建 immutable 版本（revision/predecessor/timestamp/origin）；conflicting save 拒绝；override 记 base；升级标 Default updated —— `10-sessions-prompts.md §versions`。
- [ ] 装配顺序：shared+role → 三 team sections → subscriptions/replies → dispatch/work → admission（main only）→ handoff → environment → policy（main only）—— `10-sessions-prompts.md §assembly`。
- [ ] 优先级：工具/host 强制 > built-in/generation > matched rules（per-ticket）> team sections/skills > 外部文本（数据，永不改规则）—— `10-sessions-prompts.md §precedence`。

### 2.9 面板与 HTTP API

- [ ] 三问题顺序：要不要我 → 健康吗 → 一件工作的历史 —— `11-panel-http.md §three-questions`。
- [ ] Tabs：Overview / Source / Trigger / Eval / Execution / Settings —— `11-panel-http.md §tabs`。
- [ ] Overview：需行动 Ticket + 系统问题 + 24h 活动计数 + Executor load —— `11-panel-http.md`。
- [ ] Settings 组：Sources / Eval（每 Source allowed+sync+admission+bot）/ Execution / Prompts / Deployment —— `11-panel-http.md §settings`。
- [ ] 搜索：按 Trigger 文本/id/Feishu message id/stored link；Thread 按 `#12`/URL；Ticket 按 title/匹配 Thread —— `11-panel-http.md §search`。
- [ ] HTTP API：同源 + `x-orchestrator-request: 1` header + `127.0.0.1`/`localhost`；状态码 400/404/409/500 —— `11-panel-http.md §api`。
- [ ] 路由：`GET state` / `GET settings` / `PUT settings/<block>` / `GET sources` / `POST executor/pause` / `POST setup/check` / `POST policy-sync/<id>` / `GET|PUT prompts` / `POST tickets/<id>/retry|stop` —— `11-panel-http.md §routes`。
- [ ] 健康项分组：`runtime` / `models` / `evals` / `assessment` / `launch` / `delivery` / `relay` / `notice` / `reply` / `worker` / `github` / `feishu` / `history` / `sync:<id>` —— `11-panel-http.md §health`。
- [ ] 面板原则：聚焦重要 / 精确定义 / 一处一概念 / 域结构 / 诚实状态 / 默认安静 / 一致控件 —— `11-panel-http.md §principles`。

### 2.10 部署与运维

- [ ] stateDir：数据库 + 锁 + `worker-bin/` 命令 shim + `worker-gitconfig/` + 默认 `work/` Ticket 目录 —— `06-executor.md §statedir`、`11-panel-http.md`。
- [ ] setup：探活仓库 + agent 推送权 + 写 worker env + 检查 git 只用 agent 凭据；剥离含 KEY/PASSWORD/SECRET/TOKEN 的 env —— `06-executor.md §setup`。
- [ ] worker-bin shim：`gh`/`lark-cli` 重定向到 agent CLI；blocked commands 失败；shim 自移 PATH —— `06-executor.md §shims`。
- [ ] worker-gitconfig：替换 global+system git 配置；选 agent CLI 为 github.com 凭据 helper；SSH→HTTPS 重写；LFS filter；删 `GH_TOKEN`/`GITHUB_TOKEN`；不设 commit identity；不提示凭据 —— `06-executor.md §gitconfig`。
- [ ] service 脚本 `scripts/superagent start|stop|restart|status`：tmux 会话、最多等 60 秒 ready、拒绝重复实例、退出码 0/1/2/3 —— `15-rollout.md §service-script`。
- [ ] 时序检查：每小时 cron 在另一个 profile 跑 `status`，按退出码决定重启/通知 —— `15-rollout.md §hourly-check`。
- [ ] 配置 reload：除 `stateDir` 外全部 volatile；面板 save 改 setup 字段→restart；account/setup pending→`restarting: true` —— `11-panel-http.md §reload`。

### 2.11 Sherry 集成（原型没有，Python 版独有）

- [ ] Ticket ↔ TaskFlow flow 映射：一 Ticket = 一 flow；step = Executor 的子任务；finish gate 复用 Sherry 的 DAG/证据/回归门 —— `12-sherry-integration.md §ticket-flow`。
- [ ] Executor Session = Sherry subagent run：`spawn_subagent_direct` 起 worker；`run_timeout_seconds` 用 Ticket 的 prepare+work 预算；completion judge 复用 —— `12-sherry-integration.md §executor-subagent`。
- [ ] `runtime/hooks.py` 接缝：superagent 注册 auto-turn trigger 让 idle Ticket 被 Source 唤醒；注册 WS task table 让面板状态可见 —— `12-sherry-integration.md §hooks`。
- [ ] `agent/state_port.py`：truncate/heal 用于 Ticket 重启时的 Executor Session 恢复 —— `12-sherry-integration.md §state-port`。
- [ ] HITL 重定向：worker 的 HITL 被 superagent 的 per-ticket 准入替代；worker 内 `question`/`exit_plan_mode` 在无人模式拒绝（Sherry 已有 headless 行为）—— `12-sherry-integration.md §hitl`。
- [ ] 长时无人续跑：superagent 的 Ticket 状态机替代 Sherry 的 `max_continuation_turns=15`；一个 Ticket 内部仍受 Sherry 续跑上限，但 Ticket 跨越多 Session 由 Trigger 唤醒 —— `12-sherry-integration.md §continuation`。
- [ ] 旁进程部署：superagent 独立 Python 进程，与 Sherry 后端通过 SQLite + `runtime/hooks` 回调通信；不污染主后端重启周期 —— `12-sherry-integration.md §process`。

## 3. 非目标（详见 14-non-goals.md）

- **不**复刻 DeepSeek Harness 的 Session/preset/inbox 协议本身——用 Sherry 替代。
- **不**做 React/TSX 面板——用 Sherry Nuxt 前端或独立 MVP。
- **不**做 GitHub Enterprise Server 支持（原型也不支持）。
- **不**做跨 Source 身份映射（原型明确拒绝，作为 prompt 文本）。
- **不**做"跨天元目标树"（原型也没有，留作未来工作）。
- **不**在第一版做存储清理（原型也未做，"Nothing deletes old records yet"）—— 留作 P2。

## 4. 命名约定

- Python 包：`superagent/`（顶级，与 `agent/` 平级）。
- 子模块：`superagent/core/`、`superagent/app/`、`superagent/sources/`、`superagent/executor/`、`superagent/infra/`、`superagent/panel/`。
- 类型名：snake_case Python 模块 + PascalCase 类（`Engine`、`Ticket`、`SourceKind`）；Enum 用 SCREAMING_SNAKE。
- 文件名：snake_case（`engine.py`、`tickets.py`）。
- 配置键、Prompt key、Effect kind、Health entry 名：**保留原型字符串**（`runtime/shared`、`launch`、`runtime`、`sync:<id>`）以便 prompt 文本与运维脚本复用。
- 环境变量：保留 `DSH_SUPERAGENT_*` 前缀（原型 prompt 文本依赖）；Sherry 自有变量用 `SHERRY_SUPERAGENT_*`。
- 数据库表/列名：保留原型名（`trigger`、`disposition`、`outbox` 等）。

## 5. 该册验收门

- 本册清单覆盖原型 `docs/000-008` 全部功能项，无遗漏。
- 每个复刻项在后续分册有对应设计章节与测试用例编号。
- 非目标与命名约定已固化，开工期间不漂移。
