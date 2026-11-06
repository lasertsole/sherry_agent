# 11 面板与 HTTP API

复刻原型 `src/app/http.ts` + `src/app/api.ts` + `src/app/views/` + `src/client/` + `005-panel.md` + `006-deployment.md §Management API`。原型面板是 React/TSX 嵌入 Harness；Python 版用 Sherry 的 Nuxt 4 前端加一个页面，后端 HTTP API 用 Python（Robyn 或 ASGI）。

## 1. 三问题与面板哲学

面板打开按序答三问题（000/005）：

1. 要不要我？（需人行动的 Ticket + 系统问题）
2. 健康吗？在干对的事？（Source/Executor 状态）
3. 一件工作的历史？（Trigger → Eval → Ticket）

**七原则**（005 §Principles）：聚焦重要 / 精确定义 / 一处一概念 / 域结构 / 诚实状态 / 默认安静 / 一致控件。每元素答一个问题否则删；数字只在改人决策时出现；定义可校验互斥全覆盖；healthy 不占位；问题可行动（一次网络错不够）；一行解释 + (?) tooltip；破坏性操作要确认。

## 2. 前端方案

**MVP**：Sherry Nuxt 前端加 `/superagent` 页面（`client/app/pages/superagent.vue` 或独立路由），用现有 XUI 组件库（@xui/ui-desktop）。后端 HTTP API 在 superagent 进程。

**理由**：

- Sherry 已有 Nuxt 4 + XUI，复用组件（表单/分页/dialog/toast）。
- 不引入 React/TSX 工具链（原型依赖 Harness 的 client UI）。
- 单一前端入口，与 Sherry 主界面一致。
- 面板只调 HTTP API（loopback），无共享状态。

**降级**：若不想动 Sherry 前端，superagent 进程自带一个静态 HTML + vanilla JS MVP（无构建链），仅 Overview + Settings。P2 再迁 Nuxt。

## 3. Tabs（对应 005 §Tabs）

| Tab | 显示 |
|---|---|
| Overview | 需行动 Ticket + 系统问题 + 24h 活动计数 + Executor load。每块链详细页 |
| Source | Source state + setup 问题 + 非零计数；filtered Triggers + dropped 消息（24h） |
| Trigger | Thread 按 Subscribed/Evaluating/New Triggers 24h/All 过滤；每 Thread 显示 Trigger（newest first）+ Decision + Subscription |
| Eval | Decision + reason + matched rules |
| Execution | Executor state + Pause + Ticket 按 Open/Pending/Working/Waiting/Ended 过滤；Ticket 详情：actions/authorization/waiting Inputs/Subscriptions/Assignments |
| Settings | Sources/Eval/Execution/Prompts/Deployment；每 group 含 save history |

setup 未成功前 Overview 显示缺什么 + 链设置。

### 3.1 单记录阅读

- dropped 消息链 Ticket 或 Thread（无 Ticket 时）；Source 页解释对话消失还是 Source 不听。
- 人 follow-up 仍 Executor own → 标 **Delivered to Ticket**（无 Eval）。Thread 显示 handback reason + context-only Subscription。
- **Evaluate again**：发 follow-up 进 Eval 或 retry 无有效 judgement 的 Trigger（filtered/unavailable/delivered 但无非 stop Decision + 无 Executor ack）。
- Ticket 详情：originating Trigger + Decision + reason + scope + Policy version。Assignment + 次要折叠在 immediate actions 下。**Retry**：failed Input delivery 或 blocked/failed Report 后。ended Ticket 显示 outcome（Pull request merged / Stopped by a person）。

## 4. Settings groups（对应 005 §Settings）

- **Sources**：每 Source 一卡（开关 + fields + verified account facts）。GitHub 还有 receive-all 开关（Issues/pulls/comments/CI）。轮询 + 读限在 Advanced。
- **Eval**：Policy version + rules；**On each Source**（allowed list + membership sync + admission 表 + bot list）+ Eval 设置（accept mode + timeout + assessment model）。
- **Execution**：worker model + concurrency + directory root + prepare command + blocked commands。点 available variable 插入 prepare。
- **Prompts**：Edit（team sections + built-in parts + skills）/ Full prompt（装配文本 + diff）/ History。编辑的 built-in 标 changed 可 reset；模板列占位符。升级标 Default updated + diff + Keep/Use new default。
- **Deployment**：startup 问题 + Check again + stateDir + machine limits。saved limits 无 restart。Sherry Plugin Manager（若集成）同页。

## 5. 搜索

- header search 找 Trigger（文本/id/Feishu msg id/stored link 如 GitHub comment URL）。
- Feishu link 在原文缺失 → 仅当前进程 lifetime 读过后可搜。
- 找 Thread（`#12`/GitHub URL）+ Ticket（title/匹配 Thread）。

## 6. HTTP API（`superagent/panel/http.py` + `api.py`）

### 6.1 守卫

- 同源 + `x-orchestrator-request: 1` header + host = `127.0.0.1:<port>` 或 `localhost:<port>`。
- 缺 header 或外站 Origin → 403。
- body 非 JSON 或过大 → 400。
- 状态码：400（invalid value `issues[].path` / failed account check `setup` / 固定拒绝 `{code, values}` / bad body）、404（missing）、409（stale revision）、500（其余）。

### 6.2 路由（对应 006 §Management API）

| Request | Answer |
|---|---|
| `GET state` | setup（problem/feishuProblem/accounts）+ health（每 entry state/detail/at）+ load（running/queued/maxWorkers）+ paused + masterId |
| `GET settings` | blocks（policy/eval/executor 各 value+revision）+ sources（instance id/kind/installed/block value+{enabled,config}+revision+form+setup problem）+ policyVersion + environment（root+variables） |
| `GET sources` | 每 Source state（missing/off/error/setup/starting/paused/ready）+ health + setup problem + 24h stats + filtered Triggers + dropped messages（newest first，channel/preview/ticket/thread/dropped.at/dropped.reason） |
| `PUT settings/<block>` `{value, revision}` | saved + new revision + policy_changed + restarting |
| `POST executor/pause` `{paused: bool}` | paused |
| `POST setup/check` | restarting:true（setup 无可改时拒） |
| `POST policy-sync/<source_id>` | policy（Source part saved） |
| `GET prompts` / `PUT prompts` `{config, baseRevision}` | current（revision+config）+ defaults + sections + skillName + builtin + skillPrefix / new revision |
| `POST tickets/<id>/retry` / `POST tickets/<id>/stop` | 如面板 |

API save 有面板权限（可改 execution env + setup account，main Session 工具不能）。

### 6.3 settings blocks

- `source:github`：`{enabled, config: {repository, cli, observeIssues, observePulls, observeComments, observeCi}}`。
- `source:feishu`：`{enabled, config: {cli, chats: [{chatId, name, listen}], directMessages, otherChats}}`。
- `policy`：`{rules, sources: {github: {allowed, bots}, feishu: {allowed, syncChat, admission}}}`。admission row key = chat id/`other`/`direct`（Feishu）或 `issues`/`pulls`/`comments`/`ci`（GitHub）。
- `eval`：`mode`/`timeoutSeconds`/`model`。
- `executor`：`model`/`maxWorkers`/`directory`/`prepare`/`blockedCommands`。`paused` 单独 setting。

改 repo/CLI 先跑 setup check；失败 400 + `setup`；成功 account 改或 setup pending → `restarting: true`。API restart 期间暂不可用。

## 7. Views（`superagent/panel/views/`）

| 文件 | 视图 |
|---|---|
| `text.py` | `preview`（一行纯文本）/ `markdown`（无 HTML）/ `plain` / `readable` |
| `records.py` | Triggers/Tickets/Threads/Decisions/Channel links 行 + `owners_of` |
| `tickets.py` | Ticket 按 stage 列表 + Ticket 页 + Mandate basis facts + Executor load |
| `threads.py` | Thread 列表 + timeline + search + Decision list |
| `sources.py` | Source status（state + 24h counts + filtered + dropped） |
| `settings.py` | settings 页（每 block revision + Source form + env） |
| `overview.py` | overview（需人 Ticket + 问题 + 24h 流 + Executor load） |

## 8. 健康项分组（对应 006）

| Entry | Reports |
|---|---|
| `runtime` | 应用自身 |
| `models` | 等 model-budget retry 的 Session |
| `evals` | 最近 1h unavailable Eval（无后续 judgement/dismissal） |
| `assessment`/`launch`/`delivery`/`relay`/`notice`/`reply` | Effects 各 kind |
| `worker` | Executor Session 事件（首次失败后） |
| `github`/`feishu`/`history` | Source 轮询/事件/历史恢复 |
| `sync:<source id>` | allowed-list 同步 |

## 9. 国际化

- 面板文案：`superagent/panel/locales.py`（zh + en，对应原型 `src/client/locales.ts`）。
- 术语表：001 §Glossary 的中英对照，面板 `?` 按钮开。
- agent 发给人 的消息用平实语言，不用域术语。
- Sherry 四语言 README（en/zh/ja/ko）对齐：`wiki/super-agent/` 四份 README 通过 `scripts/check_docs_parity.py`。

## 10. 配置 reload

- 除 `stateDir` 外全 volatile。
- 限值用时读最新（无 restart）。
- 改 Source setup 字段（account CLI）或 setup pending → restart superagent 进程。
- Sherry Plugin Manager（若集成）改 → 同面板效果。

## 11. Sherry 前端集成

- Sherry Nuxt 加路由 `/superagent`（侧边栏入口）。
- 页面用 XUI 组件（`xui-table`/`xui-form`/`xui-dialog`/`xui-tabs`）。
- API 调用用 Sherry 现有 `bridge`（或新 `bridge/superagent.ts`），指向 superagent 进程 loopback。
- 状态轮询：`GET state` 每 2 秒（与 Passes 同频）。
- 破坏性操作（stop/retry/unsubscribe）用 `xui-dialog` 确认。

## 12. 该册验收门

- HTTP API 全部 §6.2 路由实现 + §6.1 守卫（同源/header/loopback/状态码）。
- 6 个 Tabs 视图齐备（§3 + §7）。
- Settings 5 组齐备（§4）。
- 搜索：Trigger/Thread/Ticket 三类。
- 健康项分组（§8）全 entry 可读。
- i18n：zh + en 文案完整；术语表 001 Glossary 全覆盖。
- Sherry Nuxt `/superagent` 页面可访问 + 调 API + 轮询 state。
- `tests/superagent/panel/test_http.py` 镜像 `tests/http.spec.ts`（同源/loopback/body limit/path/query/状态码）。
- `tests/superagent/panel/test_views.py` 镜像 `tests/views.spec.ts`。
- `tests/superagent/panel/test_timeline.py` 镜像 `tests/timeline.spec.ts`。
- 前端测试：`client/app/pages/superagent.spec.ts`（XUI 组件 + API mock）。
