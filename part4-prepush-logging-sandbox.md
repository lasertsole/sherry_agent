# 十五、Pre-push 门禁：重操作从 pre-commit 分离

## 背景

当前所有质量门禁都挂在 `pre-commit` 阶段。其中 basedpyright（全项目类型检查）、vue-tsc（前端类型检查）、dpdm（循环依赖扫描）是重操作，每次 `git commit` 都跑会拖慢提交节奏。开发者倾向于用 `--no-verify` 跳过，反而失去所有门禁。

**目标：** 将快操作留在 pre-commit（每次提交即时反馈），重操作移到 pre-push（推送前一次性检查），确保每次 push 必经全量门禁。

## 分层策略

| 阶段       | 定位          | 运行时机          | 耗时目标 | 包含的 hook                                                      |
| ---------- | ------------- | ----------------- | -------- | ---------------------------------------------------------------- |
| pre-commit | 快速格式+风格 | 每次 `git commit` | < 5 秒   | ruff, ruff-format, frontend-fix, eslint, commitlint              |
| commit-msg | 提交信息规范  | 每次 `git commit` | < 1 秒   | commitlint                                                       |
| pre-push   | 全量类型+架构 | 每次 `git push`   | < 60 秒  | basedpyright, vue-tsc, dpdm, knip(规划中), import-linter(规划中) |

### 为什么不把所有东西都放 pre-commit

| 问题       | 表现                                             |
| ---------- | ------------------------------------------------ |
| 提交慢     | basedpyright + vue-tsc 跑 10-30 秒，打断思路     |
| 开发者绕过 | 频繁用 `--no-verify` 跳过，连 ruff/eslint 也丢了 |
| 重复执行   | 连续 3 次 commit 跑 3 次 vue-tsc，但代码几乎没变 |

### 为什么不把所有东西都放 pre-push

| 问题              | 表现                                               |
| ----------------- | -------------------------------------------------- |
| 格式问题积累      | ruff-format / prettier 没跑，commit 间代码风格混乱 |
| commit 信息不规范 | commitlint 不跑，commit 历史不一致                 |

## hook 分配矩阵

### pre-commit（快，每次提交）

| hook           | 做什么                           | 耗时 | `stages`       |
| -------------- | -------------------------------- | ---- | -------------- |
| `ruff`         | Python lint（changed files）     | ~1s  | `[pre-commit]` |
| `ruff-format`  | Python 格式检查（changed files） | ~1s  | `[pre-commit]` |
| `frontend-fix` | prettier --write + eslint --fix  | ~3s  | `[pre-commit]` |
| `eslint`       | eslint check-only                | ~2s  | `[pre-commit]` |

### commit-msg（快，每次提交）

| hook         | 做什么              | 耗时 | `stages`       |
| ------------ | ------------------- | ---- | -------------- |
| `commitlint` | Angular commit 规范 | <1s  | `[commit-msg]` |

### pre-push（重，每次推送）

| hook            | 做什么                     | 耗时    | `stages`     |
| --------------- | -------------------------- | ------- | ------------ |
| `basedpyright`  | Python 全项目类型检查      | ~10-30s | `[pre-push]` |
| `vue-tsc`       | 前端全项目类型检查         | ~10-20s | `[pre-push]` |
| `dpdm`          | 前端循环依赖检测           | ~2-5s   | `[pre-push]` |
| `knip`          | 前端死代码检测（规划中）   | ~5-10s  | `[pre-push]` |
| `import-linter` | 后端层级依赖契约（规划中） | ~2-5s   | `[pre-push]` |

## 配置变更

### `.pre-commit-config.yaml` 更新

```yaml
# 在 default_install_hook_types 加入 pre-push
default_install_hook_types: [pre-commit, commit-msg, pre-push]

repos:
  # ── pre-commit: 快速格式 + 风格 ──────────────────────────

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.6
    hooks:
      - id: ruff
        stages: [pre-commit]
      - id: ruff-format
        args: [--check]
        types_or: [python, pyi, jupyter]
        stages: [pre-commit]

  - repo: local
    hooks:
      # Phase 1: auto-fix
      - id: frontend-fix
        name: prettier + eslint (auto-fix)
        entry: bash scripts/hooks/frontend-fix.sh
        language: system
        require_serial: true
        files: ^client/
        types_or: [ts, tsx, vue, javascript, jsx, css, scss, json]
        stages: [pre-commit]
      # Phase 2: eslint check-only
      - id: eslint
        name: eslint (check)
        entry: bash scripts/hooks/frontend-style.sh eslint
        language: system
        require_serial: true
        files: ^client/
        types_or: [ts, tsx, vue, javascript, jsx]
        stages: [pre-commit]

  # ── commit-msg: 提交信息规范 ─────────────────────────────

  - repo: local
    hooks:
      - id: commitlint
        name: commitlint (angular)
        entry: bash scripts/hooks/commitlint.sh
        language: system
        stages: [commit-msg]

  # ── pre-push: 全量类型 + 架构检查 ────────────────────────

  - repo: local
    hooks:
      # Python 全项目类型检查
      - id: basedpyright
        name: basedpyright (types, pre-push)
        entry: uv run --no-sync basedpyright
        language: system
        types: [python]
        pass_filenames: false
        require_serial: true
        stages: [pre-push]

      # 前端全项目类型检查
      - id: vue-tsc
        name: vue-tsc (client types, pre-push)
        entry: bash scripts/hooks/frontend-style.sh vue-tsc --noEmit -p .nuxt/tsconfig.app.json
        language: system
        pass_filenames: false
        require_serial: true
        files: ^client/.*\.(ts|tsx|vue|js|mjs|json)$
        stages: [pre-push]

      # 前端循环依赖检测
      - id: dpdm
        name: dpdm (circular deps, pre-push)
        entry: bash scripts/hooks/dpdm.sh
        language: system
        pass_filenames: false
        require_serial: true
        files: ^client/.*\.(ts|tsx|vue|js|mjs)$
        stages: [pre-push]

      # 前端死代码检测（规划中，实现后取消注释）
      # - id: knip
      #   name: knip (unused code, pre-push)
      #   entry: bash scripts/hooks/knip.sh
      #   language: system
      #   pass_filenames: false
      #   require_serial: true
      #   files: ^client/.*\.(ts|tsx|vue|js|mjs)$
      #   stages: [pre-push]

      # 后端层级依赖契约（规划中，实现后取消注释）
      # - id: import-linter
      #   name: import-linter (layer contract, pre-push)
      #   entry: bash scripts/hooks/import-linter.sh
      #   language: system
      #   pass_filenames: false
      #   require_serial: true
      #   files: ^server/.*\.py$
      #   stages: [pre-push]
```

### 关键变更点

| 变更                            | 原值                       | 新值                                 | 理由                    |
| ------------------------------- | -------------------------- | ------------------------------------ | ----------------------- |
| `default_install_hook_types`    | `[pre-commit, commit-msg]` | `[pre-commit, commit-msg, pre-push]` | 安装 pre-push hook      |
| `basedpyright` `stages`         | 无（默认 pre-commit）      | `[pre-push]`                         | 类型检查慢，移到 push   |
| `basedpyright` `pass_filenames` | 无                         | `false`                              | pre-push 阶段检查全项目 |
| `vue-tsc` `stages`              | 无（默认 pre-commit）      | `[pre-push]`                         | 类型检查慢，移到 push   |
| `dpdm` `stages`                 | 无（默认 pre-commit）      | `[pre-push]`                         | 全树扫描慢，移到 push   |
| `ruff` / `ruff-format`          | 无（默认 pre-commit）      | `[pre-commit]`（显式标注）           | 快，留在 commit         |
| `frontend-fix` / `eslint`       | 无（默认 pre-commit）      | `[pre-commit]`（显式标注）           | 快，留在 commit         |

### `pass_filenames: false` 对 pre-push 的意义

pre-commit 的 `pre-push` 阶段默认传入**即将推送的文件列表**。但 basedpyright / vue-tsc / dpdm 需要检查**整个项目**（类型推断和 import 树是全局的），所以必须设 `pass_filenames: false`。

### 安装方式

```bash
# 首次安装（或重新安装）
uv run pre-commit install --hook-type pre-commit
uv run pre-commit install --hook-type commit-msg
uv run pre-commit install --hook-type pre-push

# 或一条命令安装所有 hook 类型
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

`default_install_hook_types` 配置后，`pre-commit install` 不带参数也会安装全部三种 hook。

## 执行流程

```
git commit -m "feat(x): add y"
  │
  ├── pre-commit hooks（< 5s）
  │     ├── ruff（changed .py files）
  │     ├── ruff-format --check（changed .py files）
  │     ├── frontend-fix（changed client/ files, auto-fix）
  │     └── eslint（changed client/ files）
  │
  └── commit-msg hooks（< 1s）
        └── commitlint

# ─── 如果 pre-commit 失败，commit 被拒绝 ─────────────────

git push origin main
  │
  └── pre-push hooks（< 60s）
        ├── basedpyright（全项目 Python 类型）
        ├── vue-tsc（全项目前端类型）
        ├── dpdm（前端循环依赖）
        ├── knip（前端死代码，规划中）
        └── import-linter（后端层级契约，规划中）

# ─── 如果 pre-push 失败，push 被拒绝 ─────────────────────
```

## 与 CI 的关系

| 层级       | 何时运行     | 检查内容                      | 失败后果 |
| ---------- | ------------ | ----------------------------- | -------- |
| pre-commit | 每次 commit  | 格式 + 风格（快）             | 拒绝提交 |
| pre-push   | 每次 push    | 类型 + 架构 + 死代码（重）    | 拒绝推送 |
| CI         | 每次 push/PR | 全量测试 + lint + 类型 + 架构 | PR 红叉  |

> pre-push 是本地到远程的最后一道门，CI 是远程的最终验证。两者检查内容重叠是有意的 — pre-push 让开发者在 push 前就发现问题，避免 push → CI 红叉 → 修复 → 再 push 的往返。

## 涉及文件清单

| 操作 | 文件                                                         |
| ---- | ------------------------------------------------------------ |
| 编辑 | `.pre-commit-config.yaml`（加 pre-push hooks + stages 标注） |

---

# 十六、Ruff T20：禁用 print，强制使用 loguru

## 背景

项目使用 `loguru` 作为日志框架（`from loguru import logger`），但代码库中仍有 `print()` 调用。`print()` 的问题：

1. **无日志级别** — 无法区分 info / warning / error
2. **无上下文** — 不带时间戳、模块名、行号
3. **无法关闭** — 生产环境无法按级别过滤
4. **无法重定向** — 不走 logging handler，日志收集系统收不到

需要在 lint 阶段直接禁止 `print()`，强制统一使用 `logger`。

## 思路

ruff 的 `T20`（源自 flake8-print）静态检测所有 `print()` 和 `pprint()` 调用。加入 ruff `select` 列表即可，无需额外依赖或配置。

## 配置

### `pyproject.toml`

```toml
[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "UP", "S110", "T20"]
```

### 排除规则

部分场景需要使用 `print`（CLI 工具、调试脚本），通过 `per-file-ignores` 豁免：

```toml
[tool.ruff.lint.per-file-ignores]
# 调试/CLI 脚本允许 print
"scripts/debug/*" = ["T20"]
# __main__.py 中的启动提示允许 print（如环境检查）
"server/__main__.py" = ["T20"]
# 测试文件允许 print（调试输出）
"tests/**" = ["T20"]
# Vendored 代码
"skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_*/**" = ["T20"]
"models/STT_model/utils/*" = ["T20"]
```

### 存量处理

对现有 `print()` 调用，逐个替换为 `logger`：

```python
# ❌ T20 报错
print("Starting server...")
print(f"User {user_id} not found")
print("Error:", e)

# ✅ 替换为 loguru
from loguru import logger

logger.info("Starting server...")
logger.warning(f"User {user_id} not found")
logger.error(f"Error: {e}")
```

## 规则效果

```python
# ❌ ruff T20 拦截（pre-commit 阶段报错）
print("hello")              # T201: print() found
pprint(data)               # T203: pprint() found

# ✅ 合法
from loguru import logger
logger.info("hello")
logger.debug(f"data: {data}")

# ✅ 豁免场景（per-file-ignores）
# scripts/debug/inspect_model.py
print("Model summary:", model)  # 不报错（已豁免）
```

## 涉及文件清单

| 操作 | 文件                                                 |
| ---- | ---------------------------------------------------- |
| 编辑 | `pyproject.toml`（select 加 T20 + per-file-ignores） |
| 编辑 | 存量 `print()` 调用所在文件（逐个替换为 logger）     |

---

# 十七、ESLint no-console：禁用 console，强制使用日志工具

## 背景

前端项目已有完整的日志基础设施：

| 文件                                  | 职责                                                                                                 |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `client/app/utils/log.ts`             | `SimpleLogger` 类：`l()`(info) / `w()`(warn) / `e()`(error)，环境感知（dev 输出 console，prod 静默） |
| `client/app/composables/clientLog.ts` | 日志 composable：持久化到 IndexedDB，可在"Log Viewer → Client"面板查看                               |

但 ESLint 当前只把 `no-console` 设为 `'warn'`（警告不阻断），开发者容易忽略。需要升级为 `'error'`（硬拦截），强制所有 `console.*` 调用走日志工具。

## 思路

ESLint 的 `no-console` 规则静态检测 `console.log` / `console.warn` / `console.error` 等调用。当前配置已有豁免机制，只需把 `'warn'` 升为 `'error'`。

## 现有豁免（已配置，无需改动）

```js
// eslint.config.mjs — 已有的豁免
{
  // Log infrastructure owns console calls; tests legitimately mock/console.
  files: ['**/__tests__/**', '**/clientLog.ts'],
  rules: {
    'no-console': 'off',
    '@typescript-eslint/no-explicit-any': 'off'
  }
},
```

```ts
// client/app/utils/log.ts — 已有文件级豁免
/* eslint-disable no-console -- this file IS the log infrastructure (logger sink) */
```

## 配置变更

### `client/eslint.config.mjs`

```js
// 变更前
{
  rules: {
    'no-console': 'warn',    // ← 仅警告
    // ...
  }
}

// 变更后
{
  rules: {
    'no-console': 'error',   // ← 硬拦截
    // ...
  }
}
```

仅此一行改动，其余豁免配置已就绪。

### 豁免清单（现有，确认无遗漏）

| 豁免目标          | 豁免方式                             | 理由                                 |
| ----------------- | ------------------------------------ | ------------------------------------ |
| `**/__tests__/**` | `no-console: 'off'`（eslint config） | 测试中合法使用 console mock          |
| `**/clientLog.ts` | `no-console: 'off'`（eslint config） | 日志 composable 的底层 sink          |
| `**/log.ts`       | `/* eslint-disable no-console */`    | SimpleLogger 实现的底层 console 调用 |

## 规则效果

```ts
// ❌ eslint no-console 报错（pre-commit 阶段拦截）
console.log("hello");
console.warn("deprecated");
console.error("something went wrong", error);

// ✅ 使用日志工具
import { logUtil } from "~/utils/log";

logUtil.l("hello"); // dev 输出，prod 静默
logUtil.w("deprecated"); // dev 输出，prod 静默
logUtil.e("something went wrong", error); // dev 降级 warn，prod 走 clientLog 持久化

// ✅ 豁免场景（log.ts 内部）
/* eslint-disable no-console */
console.log(...args); // 不报错（日志基础设施内部实现）
```

## 涉及文件清单

| 操作 | 文件                                                                  |
| ---- | --------------------------------------------------------------------- |
| 编辑 | `client/eslint.config.mjs`（`no-console` 从 `'warn'` 改为 `'error'`） |

---

# 十八、沙箱防逃逸与子代理权限递减

## 背景

项目有多层沙箱和权限隔离机制，确保子代理（subagent）无法逃逸沙箱、且子代理的权限严格小于父代理。本文档记录现有架构和执行流程。

## 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                     沙箱与权限隔离体系                          │
│                                                                │
│  Layer 1: OS 沙箱 (sandbox.py)                                │
│    └── bwrap (Linux) / seatbelt (macOS)                       │
│    └── SANDBOX_POLICY: required / auto / off                  │
│                                                                │
│  Layer 2: 沙箱绕过守卫 (sandbox_guard.py)                      │
│    └── _deny_sandbox_bypass(): sandbox=False 时检查 caller_scope│
│    └── 非主会话 (subagent/background) → 直接拒绝               │
│                                                                │
│  Layer 3: 工具继承策略 (inherited_tool_policy.py)              │
│    └── caller_scope = "subagent" 印章                         │
│    └── DEFAULT_SUBAGENT_BLOCKED_TOOLS: spawn/yield 默认封禁    │
│    └── metadata["scope"] = "main_only": 不可下放的工具         │
│                                                                │
│  Layer 4: 最小权限网关 (gateway_dispatch.py)                   │
│    └── ORCHESTRATOR: read + spawn + kill + yield + send       │
│    └── LEAF: read + yield                                     │
│    └── 其他: read only                                        │
│                                                                │
│  Layer 5: 运行时隔离 (runtime_isolation.py)                    │
│    └── CWD 前缀白名单限制                                      │
│    └── 跨 runtime 生成检测 → restricted = True → 拒绝           │
│                                                                │
│  Layer 6: 人工审批 (humanInTheLoop/core.py)                    │
│    └── sandbox=False 终端/REPL 调用需人工审批（仅主会话）       │
└──────────────────────────────────────────────────────────────┘
```

## Layer 1：OS 沙箱

**文件：** `agent/tools/pub_base/sandbox.py`

### 策略矩阵

| `SANDBOX_POLICY` | `sandbox=True`                                                        | `sandbox=False`                    |
| ---------------- | --------------------------------------------------------------------- | ---------------------------------- |
| `required`       | bwrap/seatbelt 包装执行；backend 不可用 → `RuntimeError`              | **直接拒绝**（工具层，无审批路径） |
| `auto`（默认）   | bwrap/seatbelt 包装执行；backend 不可用 → 降级为非沙箱 + 一行 warning | 需 HITL 人工审批（humanInTheLoop） |
| `off`            | 从不沙箱                                                              | 从不沙箱，从不审批                 |

### 平台后端

| 平台    | 后端              | 工具           | 实现                                            |
| ------- | ----------------- | -------------- | ----------------------------------------------- |
| Linux   | `BwrapBackend`    | `bwrap`        | `sandbox_bwrap.py` — bubblewrap                 |
| macOS   | `SeatbeltBackend` | `sandbox-exec` | `sandbox_seatbelt.py` — Seatbelt                |
| Windows | 无                | —              | 降级为非沙箱（auto）或 RuntimeError（required） |

### 关键设计

- `read_policy()` 每次调用都读环境变量（不缓存），运行时切换策略立即生效
- 后端懒加载（`ImportError` → 不可用，不崩溃）
- `OFF` 策略跳过 probe（不触发子进程/文件系统操作）

## Layer 2：沙箱绕过守卫

**文件：** `agent/tools/pub_base/sandbox_guard.py`

### `_deny_sandbox_bypass(sandbox: bool)`

```
if sandbox is True → return（不拦截，正常沙箱执行）
if sandbox is False:
  ├── caller_scope != "main" → raise ToolException("沙箱绕过仅限主会话人工审批")
  └── SANDBOX_POLICY == "required" → raise ToolException("SANDBOX_POLICY=required 拒绝未沙箱执行")
```

### 消费方

| 工具        | 类                    | 调用位置                      |
| ----------- | --------------------- | ----------------------------- |
| 终端命令    | `SafeShellTool`       | `_run()` / `_arun()` 开头调用 |
| Python REPL | `TimedPythonREPLTool` | `_run()` / `_arun()` 开头调用 |

### caller_scope 的来源

| scope 值       | 由谁设置                                          | 能否 `sandbox=False`    |
| -------------- | ------------------------------------------------- | ----------------------- |
| `"main"`       | 默认（主会话工具不印章）                          | ✅ 可申请（需人工审批） |
| `"subagent"`   | `apply_tool_policy()` 在 inherited_tool_policy.py | ❌ 直接拒绝             |
| `"background"` | `heartbeat.py` 手动印章                           | ❌ 直接拒绝             |

## Layer 3：工具继承策略

**文件：** `agent/tools/subagent/spawn/inherited_tool_policy.py`

### `apply_tool_policy(all_tools, tool_allow, tool_deny, blocked_tools)`

执行顺序（前一步拦截的不会被后一步放行）：

```
1. metadata["scope"] == "main_only" → 丢弃（不可下放，不可被 allow-list 重新授予）
2. name in deny_set → 丢弃
3. allow_set 非空且 name not in allow_set → 丢弃
4. 存活工具 → metadata["caller_scope"] = "subagent"（印章）
```

### DEFAULT_SUBAGENT_BLOCKED_TOOLS

```python
DEFAULT_SUBAGENT_BLOCKED_TOOLS = [
    "sessions_spawn",   # 防止子代理递归生成子代理
    "sessions_yield",   # 防止子代理抢占父代理控制权
]
```

- **fallback only**：当 `tool_deny is None` 时才生效
- **ORCHESTRATOR 例外**：ORCHESTRATOR 角色可以解锁 spawn/yield（管理自己的子代理），通过从 deny 列表中移除实现

### main_only 工具

`metadata["scope"] = "main_only"` 标记的工具永远不会下放给子代理，包括：

- `memory` — 记忆管理
- `skill_manage` — 技能管理
- `sessions_kill` — 终止其他子代理
- `sessions_steer` — 转向其他子代理

> 这些工具即使被加入 allow-list 也不会生效 — main_only 门禁在 allow/deny 逻辑之前执行。

## Layer 4：最小权限网关

**文件：** `agent/tools/subagent/spawn/gateway_dispatch.py`

### `resolve_least_privilege_scopes(agent_id, role)`

| 角色         | scopes                                                |
| ------------ | ----------------------------------------------------- |
| ORCHESTRATOR | `subagent:read` + `spawn` + `kill` + `yield` + `send` |
| LEAF         | `subagent:read` + `yield`                             |
| 其他         | `subagent:read`                                       |

> 最小权限原则：每个子代理只获得其角色必需的最小 scope 集合。

## Layer 5：运行时隔离

**文件：** `agent/tools/subagent/spawn/runtime_isolation.py`

### CWD 前缀白名单

```python
validate_cwd_restriction(cwd, allowed_prefixes)
# cwd 必须在 allowed_prefixes 之一下，否则拒绝
```

- 子代理的工作目录必须是父代理工作目录的子路径
- 防止 `..` 遍历逃逸到父目录之外

### 跨 runtime 生成检测

```python
if agent_id != "main" and agent_id != config.runtime:
    config.restricted = True  # → validate_runtime_isolation() 拒绝
```

- 防止不同 runtime 的代理互相生成子代理

## Layer 6：人工审批（HITL）

**文件：** `agent/middlewares/humanInTheLoop/core.py`

### `_sandbox_bypass_interrupt()`

- 仅对 `sandbox=False` 的 terminal / python_repl 调用触发
- 仅在主会话（`caller_scope == "main"`）生效
- YOLO 模式跳过审批
- 子代理 / 后台代理在 Layer 2 已被拒绝，不会到达此层

## 完整执行流程

### 子代理使用终端命令

```
子代理 LLM 调用 terminal 工具，sandbox=True（默认）
  │
  ├── Layer 3: apply_tool_policy()
  │     └── 工具已被印章 metadata["caller_scope"] = "subagent"
  │
  ├── Layer 2: _deny_sandbox_bypass(sandbox=True)
  │     └── sandbox=True → 直接放行（不检查 scope）
  │
  └── Layer 1: OS 沙箱
        └── SANDBOX_POLICY=auto + backend 可用 → bwrap/seatbelt 包装执行
```

### 子代理尝试 sandbox=False（逃逸尝试）

```
子代理 LLM 调用 terminal 工具，sandbox=False（尝试绕过沙箱）
  │
  ├── Layer 2: _deny_sandbox_bypass(sandbox=False)
  │     ├── caller_scope == "subagent" (≠ "main")
  │     └── raise ToolException("沙箱绕过仅限主会话人工审批")
  │
  └── ❌ 被拒绝，不会到达 OS 沙箱层或 HITL 层
```

### 主会话尝试 sandbox=False

```
主会话 LLM 调用 terminal 工具，sandbox=False
  │
  ├── Layer 2: _deny_sandbox_bypass(sandbox=False)
  │     ├── caller_scope == "main" → 不拒绝
  │     └── SANDBOX_POLICY == "auto" → 不拒绝，继续
  │
  ├── Layer 6: HITL _sandbox_bypass_interrupt()
  │     ├── YOLO 模式 → 跳过审批，直接执行
  │     └── 非 YOLO → 弹出人工审批，等待用户确认
  │         ├── 用户同意 → 非沙箱执行
  │         └── 用户拒绝 → ToolException
  │
  └── Layer 1: sandbox=False → 不包装 OS 沙箱，直接执行
```

### 后台代理（heartbeat）尝试 sandbox=False

```
后台代理 LLM 调用 terminal 工具，sandbox=False
  │
  ├── Layer 2: _deny_sandbox_bypass(sandbox=False)
  │     ├── caller_scope == "background" (≠ "main")
  │     └── raise ToolException("沙箱绕过仅限主会话人工审批")
  │
  └── ❌ 被拒绝（后台代理无 HITL 中间件，永远无法绕过沙箱）
```

## 权限递减证明

| 权限维度       | 主会话 (main)           | 子代理 (subagent)                                | 后台代理 (background) |
| -------------- | ----------------------- | ------------------------------------------------ | --------------------- |
| sandbox=False  | ✅ 可申请（需人工审批） | ❌ 直接拒绝                                      | ❌ 直接拒绝           |
| spawn/yield    | ✅ 可用                 | ❌ 默认封禁（ORCHESTRATOR 例外）                 | ❌ 默认封禁           |
| main_only 工具 | ✅ 可用                 | ❌ 不可下放                                      | ❌ 不可下放           |
| CWD 范围       | 任意                    | 父代理工作目录子路径                             | 固定工作目录          |
| HITL 审批      | ✅ 有中间件             | ❌ 无中间件                                      | ❌ 无中间件           |
| scopes         | 全部                    | read only（ORCHESTRATOR +spawn/kill/yield/send） | read only             |

> 每一行的权限，子代理严格 ≤ 主会话，后台代理严格 ≤ 子代理。

## 涉及文件

| 文件                                                  | 职责                                      |
| ----------------------------------------------------- | ----------------------------------------- |
| `agent/tools/pub_base/sandbox.py`                     | OS 沙箱策略、后端 ABC、平台调度           |
| `agent/tools/pub_base/sandbox_bwrap.py`               | Linux bwrap 后端                          |
| `agent/tools/pub_base/sandbox_seatbelt.py`            | macOS Seatbelt 后端                       |
| `agent/tools/pub_base/sandbox_guard.py`               | 沙箱绕过守卫 mixin                        |
| `agent/tools/terminal.py`                             | SafeShellTool（消费 sandbox_guard）       |
| `agent/tools/python_repl.py`                          | TimedPythonREPLTool（消费 sandbox_guard） |
| `agent/tools/subagent/spawn/inherited_tool_policy.py` | 工具继承策略 + caller_scope 印章          |
| `agent/tools/subagent/spawn/gateway_dispatch.py`      | 最小权限 scope 分配                       |
| `agent/tools/subagent/spawn/runtime_isolation.py`     | CWD 限制 + 跨 runtime 检测                |
| `agent/middlewares/humanInTheLoop/core.py`            | HITL 沙箱绕过审批                         |
| `server/service/heartbeat.py`                         | 后台代理 caller_scope 印章                |
