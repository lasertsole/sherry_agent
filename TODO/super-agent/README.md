# Python 版 super-agent 落地计划

> 目标：在 Sherry 仓库内新增一个 Python 实现的 `superagent` 子系统，**完全复刻** `D:\selfProj\dsh-superagent-prototype`（DeepSeek super-agent 研究原型）的全部功能，并让 Sherry 本体充当它的 Executor，使 Sherry 从 L6（航母）跨到 L7（飞船）——具备长时无人值守、事件驱动唤醒、外部反馈闭环的自主 SWE agent 平台。
>
> 本计划组是给 Sherry 自己（或其操作者）执行用的实施蓝图，详细到模块、文件、类、字段、SQL、测试级别。所有"为什么这样设计"的理由指向原型的对应文档/源码。

## 参考源

| 资源 | 路径 |
|---|---|
| 原型仓库 | `D:\selfProj\dsh-superagent-prototype` |
| 原型文档 | `docs/000-philosophy.md` … `docs/008-cookbook.md`（按编号读） |
| 原型源码 | `src/{core,app,sources,executor,infra,client}` |
| Sherry 仓库 | `D:\selfProj\sherry_agent`（本仓库） |
| Sherry 接缝 | `runtime/hooks.py`、`runtime/data_provider.py`、`agent/tools/subagent/`、`agent/tools/taskflow/`、`server/service/turn_runner.py` |

## 阅读顺序

每篇只建立在它前面那篇之上。先读 `00-overview` 再开干。

| 文件 | 主题 | 对应原型文档 |
|---|---|---|
| [00-overview.md](00-overview.md) | 目标、范围、复刻清单、非目标、命名约定 | 000 Philosophy |
| [01-architecture.md](01-architecture.md) | Python 端口架构、模块树、依赖规则、Harness→Sherry 适配总览 | 007 Architecture |
| [02-domain-model.md](02-domain-model.md) | 域类型 → Python dataclass / enum 全表 | 001 Domain model |
| [03-engine.md](03-engine.md) | Engine + Intake/Evals/Tickets/Assignments/Replies 五部件 + 事务模型 | 007 Engine 章节 |
| [04-store.md](04-store.md) | SQLite schema、Repository、KV、迁移、生成列 | 007 Persistence 章节 |
| [05-sources.md](05-sources.md) | SourceKind 协议、GitHub/Feishu 源、轮询、Trigger 翻译 | 002、008 Cookbook |
| [06-executor.md](06-executor.md) | DshExecutor、WorkerSlots、后台作业、prepare 命令、并发 | 003 Execution |
| [07-authorization.md](07-authorization.md) | Policy、gate、admission 表、Mandate、强度、revalidation | 002 Authorization |
| [08-assessment.md](08-assessment.md) | MasterAssessor、Eval 上下文窗口、模型研判 | 002、004 |
| [09-effects-outbox.md](09-effects-outbox.md) | Effects 持久队列、Outbox 收据、重试退避、HealthBoard | 003、007 Effects |
| [10-sessions-prompts.md](10-sessions-prompts.md) | Session 适配（Harness→Sherry）、Prompts/版本、preset | 004 Prompts |
| [11-panel-http.md](11-panel-http.md) | Web 面板、HTTP 管理 API、views、同源/loopback 守卫 | 005 Panel、006 |
| [12-sherry-integration.md](12-sherry-integration.md) | Ticket↔TaskFlow、Executor↔subagent、hooks 接缝、state_port | 007 + Sherry AGENTS.md |
| [13-test-plan.md](13-test-plan.md) | 测试镜像、覆盖目标、按原型 spec 对应 | 007 Tests 表 |
| [14-non-goals.md](14-non-goals.md) | 不复刻的部分、研究原型警告、未来工作 | README 警告 |
| [15-rollout.md](15-rollout.md) | 分阶段交付里程碑与验收门 | — |

## 关键决策（开工前必须确认）

1. **包根**：`superagent/`（与 `agent/`、`server/`、`context_engine/` 并列）。Python 3.13、uv 管理、basedpyright 检查、ruff lint/format。详见 `01-architecture.md §包根与工具链`。
2. **Executor 后端**：复刻品 **不依赖 DeepSeek Harness**。原型的 `DshExecutor` + `Sessions` 走 Harness 的 Session API；Python 版用 **Sherry 自己的 subagent 系统**作为 Executor。这是最大的工程差异，详见 `06-executor.md` 与 `12-sherry-integration.md`。
3. **面板**：原型用 React/TSX 面板嵌入 Harness。Python 版用 Sherry 现有的 Nuxt 4 前端加一个新页面（或独立 FastAPI + 静态 HTML MVP），详见 `11-panel-http.md`。
4. **进程宿主**：原型是 Harness 插件。Python 版是一个**独立 Python 进程**（Robyn 或独立 ASGI），与 Sherry 后端**同进程或旁进程**——默认旁进程，避免污染主后端的重启周期。详见 `01-architecture.md §进程拓扑`。
5. **CLI 传输**：原型用 `gh` / `lark-cli` 命令行子进程。Python 版沿用同样契约（`subprocess.run` 封装），不直接调 GitHub REST API——保持与原型行为一致并保留 idempotency key 路径。

## 执行原则（从原型 000 Philosophy 继承）

- **少占人注意力，多给价值**：每个面板元素、每条消息、每个设置都要"价值 > 注意力成本"，否则删除。
- **只在被请求或规则允许时行动**；只在"人必须行动或能用结果"时发声。
- **按影响停，不按努力停**：本地改动跑到"可 review 的结果"为止；需要设计决策的停下问一个具体问题。
- **诚实汇报**：草稿标未完成、失败报失败、绝不为绕过提问而越限。

## 验收标准（总）

落地完成时应满足：

1. `superagent/` 包通过 `uv run --no-sync basedpyright superagent/` 与 `ruff check`。
2. `tests/superagent/` 镜像原型 `tests/` 全部用例（见 `13-test-plan.md`），`uv run pytest tests/superagent -q` 全绿。
3. 端到端：在一个 GitHub 测试仓库上，允许列表里的人 `@agent` 提一个 issue → 在 60 秒（一个轮询周期）内 Overview 出现 Ticket → Executor 在自己的 worktree 里跑 → 提 PR → CI 结果回灌成 Trigger → Executor 据 CI 修 → PR 合并 → Ticket 终止为 `merged`。
4. 重启韧性：杀进程后重启，running Assignment 带 recovery note 重开；idle Ticket 不丢；outbox 待发消息继续投递。
5. 文档：`wiki/super-agent/` 四语言 README 与本计划组对齐（`scripts/check_docs_parity.py` 通过）。

每个分册末尾有自己的"该册验收门"，总验收门见 `15-rollout.md`。
