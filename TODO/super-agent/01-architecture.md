# 01 架构：Python 端口、模块树、依赖规则

## 1. 包根与工具链

新增顶级包 `superagent/`，与 `agent/`、`server/`、`context_engine/` 平级。

```
sherry_agent/
├── agent/                # Sherry 本体（不动）
├── server/               # Sherry 后端（最小改动，仅 hooks 接缝）
├── context_engine/       # Sherry 记忆（不动）
├── runtime/              # Sherry 运行时（hooks.py 增加 superagent 注册）
├── superagent/           # ← 新增
│   ├── __init__.py
│   ├── core/             # 域规则，无 I/O，无外部 import
│   ├── app/              # 组合根、适配器、调度
│   ├── sources/          # Source 实现（GitHub/Feishu/webhook 模板）
│   ├── executor/         # SherryExecutor + worker slots + 后台作业
│   ├── infra/            # commands/git/paths/text/health
│   ├── panel/            # HTTP 路由 + views（Python 端；前端在 Sherry Nuxt）
│   ├── prompts/          # 内置 prompt 文本（从原型 prompts/ 移植）
│   ├── skills/           # 内置 skills（从原型 skills/ 移植）
│   └── deployment.py     # Config/Machine/resolve_deployment
├── tests/
│   └── superagent/       # 镜像原型 tests/，见 13-test-plan.md
└── scripts/
    └── superagent        # service 脚本（start/stop/restart/status）
```

**工具链**（与 Sherry 一致）：

- Python 3.13，`uv` 管理依赖；新增依赖加到 `pyproject.toml` `[project.dependencies]`：`pydantic>=2`（schema 校验，替代 zod）、`apscheduler` 或自写 Passes 调度（见下）、`aiohttp`/`httpx`（GitHub REST 备用，主路径仍走 `gh` CLI）。
- 类型检查：`uv run --no-sync basedpyright superagent/`。
- Lint/format：`uv run --with ruff ruff check superagent/ && ruff format --check superagent/`。
- 导入契约：`uv run --no-sync lint-imports` 加一条 `superagent` 的分层规则（见 §依赖规则）。
- 测试：`uv run pytest tests/superagent -q`。

**不引入** DeepSeek Harness 任何依赖；不引入 React/TSX 工具链。

## 2. 进程拓扑

原型是 Harness 插件，与 Harness 同进程。Python 版采用**旁进程**部署：

```
┌─────────────────────────┐      ┌──────────────────────────┐
│  Sherry 后端进程          │      │  superagent 进程           │
│  (Robyn, server/__main__)│      │  (Robyn 或 ASGI, 独立端口)  │
│                          │      │                            │
│  agent.core.built_agent  │◄─────┤  superagent.app.runtime    │
│  runtime/hooks.py        │ 回调 │  Engine + Sources + Effects│
│  agent/tools/subagent    │      │  SherryExecutor            │
│                          │      │                            │
│  SQLite: sherry.db       │      │  SQLite: orchestrator.db   │
└─────────────────────────┘      └──────────────────────────┘
        ▲                                   │
        │ spawn_subagent_direct (RPC)        │
        └───────────────────────────────────┘
                via runtime/hooks seam
```

**为什么旁进程**：

1. superagent 的重启周期（setup 失败、account 改动、Policy reload 触发 restart）与 Sherry 后端独立——原型明确"keep it separate from the profile you work in: the plugin changes process-wide PATH and git settings, and its process needs independent restarts and upgrades"。
2. worker-bin shim 改 `PATH`、worker-gitconfig 替换 git 配置——这些是进程级副作用，绝不能污染 Sherry 主后端（否则主后端的 `terminal` 工具会拿到 agent 凭据）。
3. superagent 自己的 SQLite + tmux 会话 + hourly check，与 Sherry 后端解耦。

**接缝**：superagent 进程通过 `runtime/hooks.py` 的注册点调用 Sherry 后端的 `spawn_subagent_direct`（起 Executor Session）、`state_port`（heal/truncate）、`turn_runner`（cancel）。Sherry 后端启动时若检测到 superagent 进程在跑，注册这些回调；否则 superagent 的 `SherryExecutor` 退化为"本地模式"（自己直接跑 LLM，不走 subagent——仅用于单测和离线场景）。

**通信协议**：进程间用 HTTP（loopback，复用 `runtime/hooks` 的现有 WS/HTTP 通道）或直接函数调用（同进程可选）。MVP 走 HTTP loopback，端口配在 `superagent.jsonc`。

## 3. 模块映射（原型 TS → Python）

下表把原型每个 TS 模块映射到 Python 文件。同名同责。详细字段见后续分册。

| 原型 TS | Python | 责任（一句话） |
|---|---|---|
| `src/core/model.ts` | `superagent/core/model.py` | 域类型 + 共享谓词 |
| `src/core/ports.ts` | `superagent/core/ports.py` | Engine 端口接口 + 域事件 |
| `src/core/engine.ts` | `superagent/core/engine.py` | Engine + 角色接口 |
| `src/core/part.ts` | `superagent/core/part.py` | `EnginePart` 基类 + `EngineOptions` |
| `src/core/intake.py` | `superagent/core/intake.py` | Trigger 接入 + 扇出 + Eval 排队 |
| `src/core/evals.py` | `superagent/core/evals.py` | Eval 批处理 + Decision |
| `src/core/tickets.py` | `superagent/core/tickets.py` | Ticket 生命周期 + Thread 跟随 |
| `src/core/assignments.py` | `superagent/core/assignments.py` | Assignment 事实 + Report |
| `src/core/replies.py` | `superagent/core/replies.py` | Channel 写入规则 |
| `src/core/stage.py` | `superagent/core/stage.py` | `ticket_stage` + `can_retry` |
| `src/core/errors.py` | `superagent/core/errors.py` | `UserError` + `USER_ERRORS` |
| `src/core/reasons.py` | `superagent/core/reasons.py` | `Reason` + `REASONS` + `reason_text` |
| `src/core/ids.py` | `superagent/core/ids.py` | `key()` 确定性 id |
| `src/core/memory.py` | `superagent/core/memory.py` | 内存仓库供测试 |
| `src/app/runtime.py` | `superagent/app/runtime.py` | 组合根 + 启停 |
| `src/app/setup.py` | `superagent/app/setup.py` | setup 探活 + `setup_changed` |
| `src/app/passes.py` | `superagent/app/passes.py` | Passes 调度器 |
| `src/app/outbox.py` | `superagent/app/outbox.py` | Outbox 收据 + reply effect |
| `src/app/notices.py` | `superagent/app/notices.py` | 通知/转发到 main Session |
| `src/app/store.py` | `superagent/app/store.py` | Store + `Receipt` |
| `src/app/store-read.py` | `superagent/app/store_read.py` | 只读查询 |
| `src/app/migration.py` | `superagent/app/migration.py` | schema 11 迁移 |
| `src/app/effects.py` | `superagent/app/effects.py` | Effects 持久队列 |
| `src/app/versions.py` | `superagent/app/versions.py` | `Versions` 快照 |
| `src/app/cache.py` | `superagent/app/cache.py` | `TextCache` LRU |
| `src/app/settings.py` | `superagent/app/settings.py` | Settings 读写 |
| `src/app/settings-history.py` | `superagent/app/settings_history.py` | `policy_history` |
| `src/app/prompts.py` | `superagent/app/prompts.py` | Prompts + builtInParts |
| `src/app/context.py` | `superagent/app/context.py` | 内置 prompt 文本读取 |
| `src/app/skill_files.py` | `superagent/app/skill_files.py` | 打包文本读取 |
| `src/app/sessions.py` | `superagent/app/sessions.py` | Session 管理（Sherry 适配） |
| `src/app/delivery.py` | `superagent/app/delivery.py` | `AgentDelivery` |
| `src/app/retry_health.py` | `superagent/app/retry_health.py` | model-budget retry 状态 |
| `src/app/role_models.py` | `superagent/app/role_models.py` | Eval/Executor model 解析 |
| `src/app/assessment.py` | `superagent/app/assessment.py` | MasterAssessor |
| `src/app/execution_environment.py` | `superagent/app/execution_environment.py` | 执行环境变量 |
| `src/app/source_context.py` | `superagent/app/source_context.py` | `source_context_of` |
| `src/app/links.py` | `superagent/app/links.py` | Source 消息链接 + candidate card |
| `src/app/cards.py` | `superagent/app/cards.py` | 紧凑 card |
| `src/app/resources.py` | `superagent/app/resources.py` | `RESOURCES` + `read_resource` |
| `src/app/tools/main.py` | `superagent/app/tools/main.py` | main Session 工具 |
| `src/app/tools/executor.py` | `superagent/app/tools/executor.py` | Executor 工具 |
| `src/app/tools/tool.py` | `superagent/app/tools/tool.py` | `Tool` 类型 + `read_tool` |
| `src/app/http.py` | `superagent/panel/http.py` | HTTP 路由 |
| `src/app/api.py` | `superagent/panel/api.py` | `Api` 类型化路由 |
| `src/app/views/*.ts` | `superagent/panel/views/*.py` | 视图 |
| `src/executor/dsh.ts` | `superagent/executor/sherry.py` | `SherryExecutor` + `WorkerSlots` |
| `src/executor/background.py` | `superagent/executor/background.py` | 后台作业管理 |
| `src/infra/commands.py` | `superagent/infra/commands.py` | `Commands` CLI 传输 |
| `src/infra/environment.py` | `superagent/infra/environment.py` | 进程环境 + shims |
| `src/infra/paths.py` | `superagent/infra/paths.py` | 路径 |
| `src/infra/text.py` | `superagent/infra/text.py` | 词数 + chat 限 + fill |
| `src/infra/health.py` | `superagent/infra/health.py` | `HealthBoard` |
| `src/sources/kind.py` | `superagent/sources/kind.py` | `SourceKind` 协议 |
| `src/sources/registry.py` | `superagent/sources/registry.py` | `SourceRegistry` |
| `src/sources/host.py` | `superagent/sources/host.py` | `SourceHost`/`SourceContext`/`Health` |
| `src/sources/poll.py` | `superagent/sources/poll.py` | 共享轮询 |
| `src/sources/github*.py` | `superagent/sources/github*.py` | GitHub 源（拆 5 文件） |
| `src/sources/feishu*.py` | `superagent/sources/feishu*.py` | Feishu 源（拆 6 文件） |
| `src/sources/admission_context.py` | `superagent/sources/admission_context.py` | assessment Thread 窗口 |
| `src/sources/feishu_image.py` | `superagent/sources/feishu_image.py` | Feishu 图片读取 |
| `src/policy.py` | `superagent/policy.py` | Policy schema/gate/revalidate |
| `src/admission.py` | `superagent/admission.py` | admission 表值 |
| `src/limits.py` | `superagent/limits.py` | 值上限 |
| `src/kv.py` | `superagent/kv.py` | `KV` 单一登记 |
| `src/setup.py` | `superagent/setup.py` | setup 错误码 |
| `src/deployment.py` | `superagent/deployment.py` | `Config`/`Machine` |
| `src/navigation.py` | `superagent/navigation.py` | Sherry 前端 session 跳转 |
| `src/index.ts` | `superagent/__main__.py` | 进程入口 |

## 4. 依赖规则（lint-imports 契约）

复刻原型的四条依赖规则，用 `lint-imports` 的 layered contract 强制：

```
[layered]
layers = [
    "superagent.core",
    "superagent.sources",
    "superagent.executor",
    "superagent.app",
    "superagent.panel",
    "superagent",
]
```

规则：

1. **`superagent.core` 不 import 外部**（除 stdlib `hashlib`）。无 I/O。类型注解可用 `typing.TYPE_CHECKING` 引 `ports`。
2. **`superagent.sources` 不 import `superagent.app`**。Source 通过 `SourceHost`/`SourceContext` 窄接口拿能力。
3. **`superagent.panel` 只 import 类型**（`superagent.app.api` 的类型、`superagent.core.errors`、`superagent.core.reasons`、`superagent.admission`、`superagent.limits`、`superagent.setup`、`superagent.sources.host`、`superagent.navigation` 的纯值）——不 import `superagent.app.runtime` 等运行时类，避免循环。
4. **一个状态一个 writer**（见 03-engine.md §一写者表）。

额外（Python 版独有）：

5. **`superagent.executor` 不 import `agent/`**——它通过 `runtime/hooks.py` 的接口调 Sherry，不直接 import Sherry 代码。这保证 superagent 可独立单测（mock hooks）。
6. **`agent/` 不 import `superagent/`**——superagent 是上层，Sherry 是执行单元；Sherry 不知道 superagent 存在，只暴露 hooks。
7. **`server/` 仅通过 `runtime/hooks.py` 与 superagent 交互**——不直接 import `superagent.app.runtime`。

## 5. 类型与 schema 校验

原型用 `zod`。Python 版统一用 **`pydantic v2`**：

- 每个 `SourceKind.schema` 是 `pydantic.BaseModel` 子类（带默认值）。
- `SettingsSchema` / `PolicySchema` / `PromptConfig` 都是 pydantic model。
- 校验失败抛 `pydantic.ValidationError`，由 HTTP 层转 400 + `issues[].path`。
- `strict=True`（拒绝未知字段，对齐原型 `.strict()`）。

**不**用 `dataclass` 做 schema 校验（dataclass 用于纯域类型，见 `02-domain-model.md`）。

## 6. 异步模型

原型是 TS 单线程事件循环 + DeepSeek Harness 异步。Python 版：

- **Engine 全同步**（原型 `All Engine methods are synchronous`）。每个命令一个事务。
- **适配器异步**：Effects handler、Sources 轮询、Session 调用、HTTP 路由都是 `async def`。
- 用 `asyncio`，不引入第三方事件循环。
- `Passes` 是一个 `asyncio.Task`，每 2 秒一轮 + 命令触发。
- subprocess（`gh`/`lark-cli`/`git`）用 `asyncio.create_subprocess_exec`。
- Sherry 后端回调（spawn subagent）走 HTTP（aiohttp client），不阻塞事件循环。

## 7. 该册验收门

- `superagent/` 包结构按 §1 建立，每个子包有 `__init__.py`。
- `lint-imports` layered contract 通过：`uv run --no-sync lint-imports`。
- `basedpyright superagent/` 无 error（warning 允许）。
- `ruff check superagent/` 无 error。
- 模块映射表（§3）每个 Python 文件存在且有 docstring 说明对应原型模块。
- 进程拓扑（§2） documented，`superagent/__main__.py` 可独立 `python -m superagent` 启动（即使只是空 runtime）。
