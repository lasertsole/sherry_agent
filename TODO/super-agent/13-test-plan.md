# 13 测试计划

镜像原型 `tests/` 全部用例（`007-architecture.md §Tests` 表）。测试是 superagent 的"功能完整复刻"判据——每条原型 spec 在 Python 版有对应 pytest。

## 1. 测试目录

```
tests/superagent/
├── conftest.py                          # fixtures: tmp stateDir, mock host, mock commands
├── core/
│   ├── test_engine.py                   # 镜像 tests/core-engine.spec.ts
│   ├── test_stage.py                    # 镜像 tests/stage.spec.ts
│   └── test_memory.py                   # 内存仓库与 SQLite 一致性
├── test_policy.py                       # 镜像 tests/policy.spec.ts
├── sources/
│   ├── test_registry.py                 # 镜像 tests/registry.spec.ts
│   ├── test_sources.py                  # 镜像 tests/sources.spec.ts（GitHub+Feishu）
│   ├── test_github_read.py              # 镜像 tests/github-read.spec.ts
│   ├── test_feishu_read.py              # 镜像 tests/feishu-read.spec.ts
│   ├── test_admission_context.py        # 镜像 tests/admission-context.spec.ts
│   ├── test_feishu_image.py             # 镜像 tests/feishu-image.spec.ts
│   ├── test_webhook.py                  # 镜像 cookbook §11
│   └── test_poll.py                     # pollThreads 行为
├── test_commands.py                     # 镜像 tests/commands.spec.ts
├── executor/
│   ├── test_background.py               # 镜像 tests/background.spec.ts
│   ├── test_sherry.py                   # 镜像 tests/runtime.spec.ts（SherryExecutor）
│   └── test_runtime_concurrency.py      # 镜像 tests/runtime-concurrency.spec.ts
├── test_bundle_patch.py                 # 镜像 tests/bundle-patch.spec.ts（Python 改 cordis.patch.yml → superagent.jsonc）
├── test_effects.py                      # 镜像 tests/effects.spec.ts
├── test_passes.py                       # 镜像 tests/passes.spec.ts
├── test_outbox.py                       # 镜像 tests/outbox.spec.ts
├── test_cache.py                        # 镜像 tests/cache.spec.ts
├── test_queue.py                        # 镜像 tests/queue.spec.ts
├── test_followups.py                    # 镜像 tests/followups.spec.ts
├── test_health.py                       # 镜像 tests/health.spec.ts
├── test_delivery.py                     # 镜像 tests/delivery.spec.ts
├── test_retry_health.py                 # 镜像 tests/retry-health.spec.ts
├── test_role_models.py                  # 镜像 tests/role-models.spec.ts
├── test_runtime.py                      # 镜像 tests/runtime.spec.ts（whole app on simulated host）
├── test_tools.py                        # 镜像 tests/tools.spec.ts
├── test_http.py                         # 镜像 tests/http.spec.ts
├── test_settings.py                     # 镜像 tests/settings.spec.ts
├── test_views.py                        # 镜像 tests/views.spec.ts
├── test_timeline.py                     # 镜像 tests/timeline.spec.ts
├── test_migration.py                    # 镜像 tests/migration.spec.ts
├── test_store.py                        # 镜像 tests/store.spec.ts
├── test_prompts.py                      # 镜像 tests/prompts.spec.ts
├── test_cards.py                        # 镜像 tests/cards.spec.ts
├── test_model_surface.py                # 镜像 tests/model-surface.spec.ts
├── test_deployment.py                   # 镜像 tests/deployment.spec.ts
├── test_environment.py                  # 镜像 tests/environment.spec.ts
├── test_index.py                        # 镜像 tests/index.spec.ts
├── test_service_script.py               # 镜像 tests/service-script.spec.ts
├── test_assessment.py                   # 镜像 tests/assessment.spec.ts
├── test_sherry_integration.py           # Python 独有：hooks 接缝
├── panel/
│   ├── test_panel_overview.py
│   ├── test_panel_source.py
│   ├── test_panel_trigger.py
│   ├── test_panel_eval.py
│   ├── test_panel_execution.py
│   ├── test_panel_settings.py
│   ├── test_panel_prompts.py
│   ├── test_panel_deployment.py
│   ├── test_panel_panel.py              # search + glossary + route memory
│   ├── test_draft.py
│   ├── test_prompt_config.py
│   ├── test_markdown.py
│   ├── test_marks.py
│   ├── test_locales.py
│   ├── test_styles.py
│   └── test_navigation.py
├── test_class_docs.py                   # 镜像 tests/class-docs.spec.ts（每个类前有 docstring）
├── test_docs.py                         # 镜像 tests/docs.spec.ts（编号文档只链前序；本地链接全解；无冲突标记；007 表列全模块+测试）
└── test_e2e.py                          # 端到端（mark system/llm_e2e）
```

## 2. fixtures（`conftest.py`）

对应原型 `tests/fixtures/`：

- `host`：模拟 Sherry host（不真起 Sherry）；spy `SuperAgentHooks` 调用。
- `panel_server` / `panel`：fake management API + 页面 around a route。
- `deployment`：resolved deployment。
- `git_env`：每个 test git 的 env（不命名 repo）。
- `tmp_state_dir`：临时 stateDir + Store。
- `mock_commands`：`Commands.run` spy（CLI mock）。

## 3. 覆盖目标

| 维度 | 目标 |
|---|---|
| 行覆盖 | ≥85%（与 Sherry 现有标准一致） |
| 域规则覆盖 | 100%（每 Engine 命令、Policy 分支、stage 行） |
| Effect kind | 6 种各有正/反例 |
| Source | GitHub + Feishu + webhook 各完整翻译/Channel/Thread/poll |
| HTTP 路由 | §6.2 全路由 + 守卫 |
| 模型表面 | tools/prompts/skills/inputs 对 schema + env + glossary，每 role size budget |
| 文档 | 编号文档只链前序；本地链接全解；模块表/测试表完整 |
| 类 docstring | 每个 class 有 docstring（basedpyright + 自定检查） |

## 4. 测试 marker

- `unit`（默认）：纯域 + Store + Tools。
- `integration`：Engine + Sources + Effects on simulated host。
- `module`：多部件协同（Passes + Effects + Engine + Outbox）。
- `system`：whole app on simulated host（`test_runtime.py`）。
- `llm_e2e`：真实 LLM 调用（`test_e2e.py`），默认不跑。
- `perf`：热路径性能（可选，superagent 不强求）。

CI gate（`tests/run_tests_split.py`）加 superagent 组：

- 组 A：core + policy + sources（unit）。
- 组 B：executor + effects + outbox + passes + health（integration）。
- 组 C：panel + http + views + runtime（system）+ perf guards。

## 5. 关键用例清单（与原型 1:1）

### 5.1 Engine 域规则（`test_engine.py`）

- 接收：stored/duplicate/filtered。
- Eval：批处理、超时重排、unavailable。
- fan-out：订阅 Ticket 收 Input；人消息到 Ticket 不进 Eval。
- resume：人消息唤醒 ended Ticket；bot/CI 不唤醒；revalidation 失败保持 ended。
- stop：人 stop 撤 Mandate + 永久 ended。
- interrupted Assignment：restart 带 recovery note。
- pause：停 dispatch/Eval/Input/reply；running turn 不打断。
- Policy 变更：通知受影响 Ticket；丢运行 Eval；重排。
- report：progress/waiting/ready/blocked/failed/dropped 全 kind。
- attach：显式指向才 attach；handback；context-only follow。
- replies：ack/answer/result/attention 规则。
- failed delivery：记 + 重试。
- dismissed problems：unavailable Decision dismiss。

### 5.2 Stage（`test_stage.py`）

7 行 stage 表逐行：person/delivery、person/answer、person/review、person/blocked,failed、working/queued、idle、working。`can_retry` 在 person/delivery|blocked|failed。

### 5.3 Policy（`test_policy.py`）

- Source scope + gate + admission 表 + Mandate revalidation + Policy version + settings 迁移 + assessment outcome。
- 强度全 case：explicit/inferred/not_admitted。
- accept mode：off/observe/auto。
- admission 三 cell × 三值。
- Source 同步（Feishu chat 成员）。

### 5.4 Sources（`test_sources.py`）

- 翻译：稳定 id、Thread、actor、human、addressed；agent 自己消息不产 Trigger。
- 一轮：Trigger 一次存储；scope 外 filtered；cursor 移动；名单人 mention → explicit。
- 一轮被打断：cursor 不动。
- send：sent、gone（410）、throw（其他）。
- Thread context：一页消息 + next cursor。
- plugin：apply 注册 kind + setupPending 报 paused；disposer 注销；loop 失败成 health。
- Source Policy + admission rows。

### 5.5 Effects（`test_effects.py`）

- coalescing：同 (kind, key) 替换 payload + 立即 due + 不变顺序。
- 并发限：每 kind concurrency。
- retry 退避：5s→300s（reply）、2s→60s（relay/notice/assessment/delivery）。
- Later：不计数失败 + 延后。
- health：连续 3 次失败报 error；下次成功自清。
- shutdown：drain 停处理 + 保留 pending。

### 5.6 Outbox（`test_outbox.py`）

- send：存收据 + 入 reply effect。
- 排序：同 Ticket 同会话保序；前序失败 3 次放行。
- gone：对话消失 → dropped channelGone；Source 不听 → channelIgnored。
- Source off：wait 不 drop。
- idempotency：重复 send 同 id 不重复发。

### 5.7 SherryExecutor（`test_sherry.py`）

- launch：prepare 成功/失败/超时；spawn_subagent 调用参数正确。
- WorkerSlots：满载 Later；wake 额外槽；wake 优先。
- 后台作业：turn 结束有 job keep slot；30 分钟杀 shell 留 subagent；handoff 释放 slot。
- restart resume：interrupted Assignment recovery note；pending launch 重排；oldest first。
- PR 跟随：claim/observe/reopen/close/end。
- transient 重试：3 次 5s→300s 后真正失败。
- worker env：DSH_SUPERAGENT_* 注入无凭据。
- HITL：worker question/exit_plan_mode/sherry-self-file fail-closed → blocked + Attention。

### 5.8 HTTP（`test_http.py`）

- 同源 + `x-orchestrator-request: 1` + loopback。
- 缺 header / 外站 Origin → 403。
- 400（invalid value `issues[].path` / setup / `{code,values}` / bad body）。
- 404（missing）、409（stale revision）、500（其余）。
- body limit、path param、query。

### 5.9 模型表面（`test_model_surface.py`）

- tools/prompts/skills/inputs 对 schema + env + glossary。
- 每 role size budget。
- built-in 文本规则（`test_cards.py`）。

### 5.10 文档（`test_docs.py`）

- 编号文档只链前序。
- 本地链接全解。
- 无冲突标记。
- 007 表列全模块 + 全测试。

### 5.11 Sherry 集成（`test_sherry_integration.py`，Python 独有）

- mock Sherry HTTP：spawn/cancel/add_messages/truncate/heal/read_state/list_jobs/kill_job/read_trajectory。
- Ticket↔flow：accept 开 flow；ready 前 finish gate；blocked 转 gate 反馈。
- 长时无人：worker 跑满续跑 cap 报 waiting → Ticket idle → Trigger 唤醒新 Assignment。
- 旁进程：superagent 挂 Sherry 正常；Sherry 挂 worker spawn failed → Ticket person/failed。

### 5.12 E2E（`test_e2e.py`，`llm_e2e` mark）

真实 GitHub 测试仓库 + Sherry 后端 + superagent 进程：

- 允许列表人 `@agent` 开 issue → 60s 内 Overview 出 Ticket。
- Executor 在 worktree 跑 → 提 PR。
- CI 结果回灌 Trigger → Executor 据 CI 修。
- PR 合并 → Ticket `merged`。
- 杀进程重启：running Assignment recovery note；idle Ticket 不丢；outbox 待发续投。

## 6. 该册验收门

- `tests/superagent/` 目录齐备，每原型 spec 有对应 pytest。
- 行覆盖 ≥85%；域规则 100%。
- CI gate 加 superagent 组（A/B/C）。
- `test_docs.py` 通过（编号文档链 + 模块表/测试表完整）。
- `test_class_docs.py` 通过（每类 docstring）。
- E2E（`llm_e2e`）在真实仓库跑通一次（验收时手动）。
