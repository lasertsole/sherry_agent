# 🧩 子代理设计：两轴角色、权限守卫与分层完成门禁

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 本文是[子代理系统 README](../../agent/tools/subagent/README.zh.md) 的设计层姊妹篇。后者是运行时与 API 参考——spawn 流水线各阶段、注册表状态机、announce 投递、工具 schema 与完整配置表。本页阐述角色模型、spawn 权限守卫与四层完成门禁背后的设计不变量，并逐层说明各自接受的失败模式。

事实来源：`agent/tools/subagent/**`（`types/`、`capabilities/`、`roles/`、`spawn/`）、`agent/tools/taskflow/step_judge.py` 与 `agent/tools/taskflow/tools/`、`agent/middlewares/subagent_completion_drain/`、`config/features/agent_side/`。下文每一条陈述均已对照该源码核实。

## 目录

- [总览](#-总览)
- [两条角色轴](#-两条角色轴)
- [功能角色：定义、加载与作用](#-功能角色定义加载与作用)
  - [定义文件](#-定义文件)
  - [失败开放加载器](#-失败开放加载器)
  - [角色驱动什么](#-角色驱动什么)
- [spawn 权限守卫](#-spawn-权限守卫)
- [完成判别器与 goal loop](#-完成判别器与-goal-loop)
- [分层完成门禁](#-分层完成门禁)
- [与其它子系统的关系](#-与其它子系统的关系)
  - [合成聚合](#-合成聚合)
- [配置](#-配置)
- [测试地图](#-测试地图)
- [局限](#-局限)

## 🎯 总览

一个被派发的 worker 由两个正交的答案描述，对应两个不同的问题：

1. **谁可以 spawn，能控制什么？**——**深度角色**（`SubagentSessionRole`），由嵌套深度推导。只有它决定 spawn 权限与控制范围。
2. **这是哪一类 worker？**——**功能角色**（`FunctionalRole`），一种显式特化。只有它决定子 LLM 档位、工具白名单与角色专属提示词段落。

完成判定随后由四层恒开机制把关，从单个子运行向外一直排到父回合：子运行的完成判别器与 goal loop、TaskFlow 步骤判别器、`taskflow_finish` 的流程门禁，以及父回合的完成 drain 程序化门禁。两条角色轴与门禁栈是本页的主题；API 明细见[模块 README](../../agent/tools/subagent/README.zh.md)。

### 🧱 不变量

1. **两条轴永不合并。** 功能角色不能授予 spawn 权限，深度角色也不能改变工具白名单或提示词。每次 spawn 都是两者的组合。
2. **`general` 是恒等角色。** 不传功能角色提示时，spawn 行为不变：不加载任何定义，LLM 档位由深度角色决定。
3. **spawn 权限有两道深度门**——组装期（工具策略求交）与调用期（工具自身的权限检查）。
4. **完成门禁恒开。** 整条栈没有任何启用/停用开关；输入只有预算与判据。
5. **失败开放只针对模型错误、无法解析的输出或不可用的查询——绝不针对否定判定。** 解析出的 `RETRY`、`BLOCK` 或失败证据行仍然照常拦截。
6. **子上下文始终独立。** 子代理从自己的空消息列表开始，永不继承父会话记录，也不存在改变这一点的模式。
7. **完成载体是声明，不是已验证结果。** 父回合收到的是子代理的自述；当会话没有通过证据时，drain 门禁会追加一条强制验证消息。

## 🧭 两条角色轴

| 轴 | 解析来源 | 决定 |
|----|----------|------|
| **深度角色**（`SubagentSessionRole`） | 嵌套深度 | spawn 权限、控制范围 |
| **功能角色**（`FunctionalRole`） | 显式提示 → `agent_id` 匹配 → 配置默认值 | 子 LLM、工具白名单、提示词段落 |

**深度角色。** `resolve_subagent_capabilities(depth, max_depth)` 把深度 `0` 映射为 `MAIN` 与 `ControlScope.CHILDREN`，把 `depth >= max_depth` 映射为 `LEAF` 与 `ControlScope.NONE`，其余映射为 `ORCHESTRATOR` 与 `CHILDREN`。权限判定的唯一谓词是 `can_spawn_children(role)`——仅对 `MAIN` 与 `ORCHESTRATOR` 为真。深度本身先查运行记录，再回退到会话键中 `:subagent:` 的出现次数（`get_subagent_depth`）。`max_spawn_depth` 默认 `2`，硬上限为 `2`。

**功能角色。** `_resolve_functional_role(hint, agent_id)` 依次尝试显式提示（未知提示记一条警告后继续下落）、`agent_id` 名称、配置的 `default_functional_role`，最后落到 `GENERAL`。`GENERAL` 在加载任何定义之前就短路，这正是无提示 spawn 保持深度行为不变的原因。

两条轴双向正交：深度 1 的 `researcher` 仍是可 spawn 的 `ORCHESTRATOR`；到达深度上限的 `general` 仍是不可 spawn 的 `LEAF`。

## 🧰 功能角色：定义、加载与作用

### 📄 定义文件

包内定义随包分发、纳入版本控制，位于 `agent/tools/subagent/roles/definitions/<name>/AGENTS.md`。可选的用户覆盖（不入库）可放在 `workspace/subagent_roles/<name>/AGENTS.md`；解析顺序为**覆盖 → 包内默认 → 无**，覆盖目录名可配置。每个文件携带 YAML frontmatter（`name`、`description`、`model_tier`、`tools`）以及追加到子提示词的 markdown 正文。`tools: inherit` 解析为“全部工具”；未知 `model_tier` 回退为 `inherit`。

| 角色 | 用途 | 生效 LLM 档位 | 角色工具集 |
|------|------|----------------|------------|
| `general` | 默认 worker；恒等角色（其包内定义永不被加载） | 深度角色 | 全部工具 |
| `researcher` | 只读的代码库与网络调研 | `auxiliary` | `read_file`、`terminal`、`web_search` |
| `executor` | 可写实现与命令执行 | `auxiliary` | `read_file`、`write_file`、`patch_file`、`terminal`、`python_repl` |
| `reviewer` | 只读的 diff 与质量审计 | `auxiliary` | `read_file`、`terminal` |

### 🛡️ 失败开放加载器

`load_role_definition(role)` 遍历候选路径，解析第一个存在的文件，并在任何失败下返回 `None`：文件不存在、frontmatter 缺失或未闭合、frontmatter 不是映射、`tools` 值非法、或文件不可读。每次解析失败都会记一条警告；调用方把 `None` 视为 `general`。加载器只在*模型、文件与解析*问题上失败开放——它永远不会把畸形的定义变成有特权的定义。`load_all_role_definitions()` 在进程内缓存结果；编辑工作区覆盖后调用 `invalidate_role_cache()`。

### ⚙️ 角色驱动什么

**LLM 选择。** 显式 `model_override` 优先；否则由非 `general` 角色的 `model_tier` 生效；否则由深度角色决定（ORCHESTRATOR → 主 LLM，LEAF → 辅助 LLM）。

```text
显式 model_override
  └─► 功能角色 model_tier（"main" | "auxiliary"）
        └─► 深度角色（ORCHESTRATOR → 主 LLM，LEAF → 辅助 LLM）
```

**系统提示词。** 非 `general` 角色（或任何非空角色描述/正文）会在 `## Your Role` 中加入 `{ROLE}` 特化行，并追加携带定义正文的 `## Role Instructions` 段落。

**工具策略。** 角色的 `tools` 列表成为白名单，默认拒绝列表随之清空，因为白名单本身已在做限制。每次 spawn 的 `extra_tools` 并入该白名单；两者都仍受无条件的 `main_only` 元数据门与任何显式拒绝列表约束。

## 🔒 spawn 权限守卫

spawn 权限在两个独立位置执行，因此任何单点泄漏都无法提权：

- **组装期（主门）。** 当最终白名单由角色白名单与 `extra_tools` 构建完成后，Phase 8.6 将其与 `can_spawn_children(role)` 求交：对任何无权 spawn 的角色，`sessions_spawn` 与 `sessions_yield` 都会从列表中剥离。继承模式下白名单为空、默认拒绝列表已拦截这两个工具，因此求交在那里是空操作。
- **调用期（纵深防御）。** `check_spawn_permission(session_id)` 先解析规范调用方会话键（子代理与 swarm 键原样使用；其它 id 补上 `agent:main:session:` 前缀），再解析深度（先查运行记录，后看会话键形状），然后解析深度角色并拒绝无权 spawn 的调用方。它返回 `(allowed, reason)` 元组、永不抛异常；工具通过既有的字符串契约渲染拒绝。

```text
组装期：最终白名单 ∩ can_spawn_children(role)   → 剥离 sessions_spawn / sessions_yield
调用期：check_spawn_permission(session_id)       → "status=forbidden"，永不抛异常
```

调用期守卫为 `sessions_spawn`（提权路径）兜底，工具与运行时 spawn 包装器都接入了它。`sessions_yield` 仅由组装期剥离保护。

## 🧪 完成判别器与 goal loop

完成判别器（`spawn/completion_judge.py`）是子代理层门禁。温度为 `0` 的辅助 LLM 接收任务、子代理最新回复（截断至 `8000` 字符）与子会话的验证证据摘要，返回 `DONE` 或 `CONTINUE` 及一句话理由；`CONTINUE` 判定还会携带一段续轮提示。解析逐级安全降级——先做 JSON 修复，再做正则扫描——任何模型错误或不可用输出都以显式失败开放理由按 `DONE` 终结。

只要某次 spawn 的预算大于一轮，goal loop 就会运行；预算取 `COMPLETION_JUDGE["goal_max_turns"]`（默认 `5`），且计入第一轮。每次 spawn 的 `goal_max_turns` 参数可覆盖它。收到 `CONTINUE` 时，判别器的提示会作为下一条 `HumanMessage` 注入**同一 checkpoint 线程**，因此续轮延续的是同一个子对话，而不是新开一个。

```text
第 1 轮 ─► 判官 DONE ─────────────────────────────────────► 终结
        └─► 判官 CONTINUE（continuation_prompt）─► 第 2 轮 ─► 判官 …
               （重复直到 DONE、续轮提示为空，或 turns_used == goal_max_turns）
```

预算耗尽后，运行仍以 `OK` 终结，并记录 `error="goal_loop_budget_exhausted"`——该循环从不把预算耗尽转成失败。续轮提示为空同样结束循环。预算为 `1` 时子代理保持单轮：根本不会调用判官。

## 🚦 分层完成门禁

四道门从子运行向外层叠。四道全部恒开；输入只有预算与判据。

| # | 层 | 门禁 | 所需输入 | 失败开放行为 |
|---|----|------|----------|--------------|
| 1 | 子运行 | 完成判别器 + goal loop（`spawn/completion_judge.py`） | 任务、最新回复、证据摘要；预算 `goal_max_turns`（默认 `5`） | 模型错误 / 无法解析 → `done` |
| 2 | 步骤 | TaskFlow 步骤判别器（`agent/tools/taskflow/step_judge.py`） | 步骤的 `validation_criteria`（缺失 → 不调用判官） | 模型错误 / 无法解析 → `pass` |
| 3 | 流程 | `taskflow_finish` 门禁 A–D（`agent/tools/taskflow/tools/taskflow_finish.py`） | DAG 状态与 flow 证据；门禁 D 另需 `todo` 与 `plan_path` | 账本不可读 / 校验器错误 → 放行 |
| 4 | 父回合 | 完成 drain 程序化门禁（`agent/middlewares/subagent_completion_drain/`） | 会话的验证证据摘要 | 查询失败 → 跳过门禁 |

**第 2 层——步骤判官。** `taskflow_resume` 注入子结果后，携带 `validation_criteria` 的步骤会被判定 `PASS` / `RETRY` / `BLOCK`。`RETRY` 会在步骤重试计数低于 `STEP_JUDGE["max_retries"]`（默认 `2`）时带着判官反馈重新派发该步骤；预算耗尽则阻塞该步骤。判据是判官的输入而非开关——没有判据的步骤永远不会送到判官面前。

**第 3 层——流程门禁。** `taskflow_finish` 在以下条件全部满足前拒绝转入 `DONE`：门禁 A 看到每一步都是 `done` 或 `blocked`，门禁 B 看不到阻塞步骤，门禁 C 看不到 `FAIL` 或 `[stale]` 的 flow 证据，并且——仅当调用方同时提供 `todo` 与 `plan_path` 时——门禁 D 通过 `SisyphusVerifier`。门禁 D 的关联关系是输入而非开关：缺少它时完成动作没有独立判定，且跳过的门禁保持静默。

**第 4 层——父回合门禁。** drain 注入排队的完成载体时，也会检查父会话的证据。载体原样注入；若会话没有通过证据，则在其后追加一条强制验证消息。该检查无条件执行——没有任何配置能关闭它——只有当证据查询本身不可用时才失败开放。

解析出的否定判定始终拦截：失败开放覆盖模型错误、无法解析的输出与不可用的查询，绝不覆盖真正的 `RETRY`、`BLOCK` 或失败证据行。门禁内部细节（熔断阈值、announce 重试、证据账本）见[失控循环防护 README](../loop-prevention/README.zh.md) 与[长期任务 README](../long-running-tasks/README.zh.md)。

## 🔗 与其它子系统的关系

| 子系统 | 跨越该边界的内容 | 详情 |
|--------|------------------|------|
| TaskFlow 引擎 | 步骤派发、`validation_criteria`、`aggregate_deps`、流程门禁 A–D | [长期任务](../long-running-tasks/README.zh.md) |
| 失控循环防护 | 完成门禁、判官预算、drain 与 announce 重试行为 | [失控循环防护](../loop-prevention/README.zh.md) |
| 上下文引擎 | 只有完成载体写入 MesMemory；子对话仅存在 checkpoint | [Context Engine README](../../context_engine/README.zh.md) |
| 运行时车道 | `SUBAGENT` 车道限制并发的子运行；超限 spawn 以 `PENDING` 排队 | [失控循环防护](../loop-prevention/README.zh.md) |
| 记忆文件 | 非空 drain 会重同步 `MEMORY.md` 与 `USER.md`（先读盘再落盘，失败开放） | [子代理系统 README](../../agent/tools/subagent/README.zh.md) |

### 🧵 合成聚合

`taskflow_run_task(aggregate_deps=True)` 标记一个合成步骤，使其派发任务文本携带依赖步骤已记录的结果。该标志存储在步骤上，`build_task_with_dep_results(step, steps, results)` 按 `depends_on` 顺序追加 `## Upstream Results` 区块，每个依赖一个 `### {step_id}` 小节，将依赖的 `child_session_key` 与 flow 的 `{child_session_key, result, result_hash}` 记录相匹配；没有记录结果的依赖写入 `no result recorded` 占位符。由于聚合在每次派发或重试时都从稳定记录重新推导，已存储的步骤任务永不被改写；没有该标志时任务文本保持不变。完整行为见[长期任务 README](../long-running-tasks/README.zh.md)。

## ⚙️ 配置

| 旋钮 | 位置 | 默认值 | 作用 |
|------|------|--------|------|
| `goal_max_turns` | `config/features/agent_side/completion_judge.py` | `5` | goal loop 轮次预算，含第一轮 |
| 每次 spawn 的 `goal_max_turns` | `sessions_spawn` 参数 | `None` → 配置值 | 覆盖单次 spawn 的预算 |
| `max_retries` | `config/features/agent_side/step_judge.py` | `2` | 步骤被阻塞前允许的 `RETRY` 次数 |
| `default_functional_role` | `SubagentConfig` | `"general"` | 提示与 `agent_id` 匹配都失败时的回退角色 |
| `roles_override_dir_name` | `SubagentConfig` | `"subagent_roles"` | 存放可选角色覆盖的工作区目录 |
| `max_spawn_depth` | `SubagentConfig` | `2` | 角色翻转为 `LEAF` 的深度（硬上限 `2`） |
| `verify_commands` | `config/features/agent_side/evidence_ledger.py` | test / lint / build / typecheck / format 命令族 | 自动记入验证证据的命令 |

## 🗺️ 测试地图

| 领域 | 测试 |
|------|------|
| 角色枚举与加载器 | `tests/agent/tools/subagent/types/test_functional_role.py`、`tests/agent/tools/subagent/roles/test_loader.py` |
| 角色驱动的 spawn 行为 | `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`、`tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |
| spawn 权限守卫 | `tests/agent/tools/subagent/test_spawn_privilege_guard.py` |
| 完成判别器与 drain | `tests/agent/tools/subagent/test_completion_judge.py`、`tests/agent/tools/subagent/test_completion_drain.py` |
| 父回合门禁 | `tests/agent/middlewares/test_completion_drain_gate.py` |
| 步骤判官 | `tests/agent/tools/taskflow/test_step_judge.py`、`tests/agent/tools/taskflow/test_resume_with_judge.py` |
| 流程门禁 | `tests/agent/tools/taskflow/test_finish_gate.py` |
| 合成聚合 | `tests/agent/tools/taskflow/test_synthesize.py`、`tests/agent/tools/taskflow/test_synthesize_e2e.py` |

## ⚠️ 局限

- 每个判官都按设计失败开放：不可达或无法解析的判官以 `done` / `pass` 终结。可用性优先于严格性——卡死的判官绝不能困住一次运行。
- `goal_max_turns` 为 `1` 时子代理单轮运行，该次 spawn 不调用完成判别器。
- 调用期权限守卫为 `sessions_spawn` 兜底；`sessions_yield` 仅靠组装期剥离。
- 门禁 D 只在同时提供 `todo` 与 `plan_path` 时启用；缺少该关联时 `taskflow_finish` 没有独立的完成判定。
- 父回合门禁依据文本证据摘要及其 `FAIL` / `[stale]` 标记推理，而非结构化账本。
- `aggregate_deps` 拼接原始依赖结果——不做摘要，因此超大结果会撑大派发任务文本。
- 子上下文独立是设计上的固定行为：不存在继承父会话记录的模式。
