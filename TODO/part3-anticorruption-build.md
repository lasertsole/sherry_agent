# 十一、import-linter：server 层级依赖契约

## 背景

server 包目前有 5 个子包：`trigger`、`service`、`queue`、`DAO`、`utils`。随着代码增长，容易出现下层模块反向依赖上层模块（如 service import trigger）的架构腐化。import-linter 可以静态分析 import 关系，在 pre-commit 和 CI 阶段自动拦截违规跨层引用。

## server 现有结构

```
server/
├── __init__.py
├── __main__.py              # 入口，组装各层
├── trigger/                 # Layer 1 — API/Transport 触发器
│   ├── core.py              # Robyn app 定义，不依赖 server 任何子包
│   ├── subagent_serialize.py # 子代理序列化（trigger 内部共享）
│   ├── channels/core.py     # Channel 触发器
│   ├── http/                # HTTP 路由处理器（20 个文件）
│   ├── ws/                  # WebSocket 处理器（5 个文件）
│   └── subagent/core.py     # 子代理触发器
├── service/                 # Layer 2 — 业务逻辑
│   ├── auto_turn.py
│   ├── env.py
│   ├── file_store.py
│   ├── heartbeat.py
│   ├── input_queue_service.py
│   ├── interrupt_marker.py
│   ├── memory.py
│   ├── messages.py
│   ├── sherry_config.py
│   ├── skill_scanner.py
│   ├── stream_dispatch.py
│   ├── stream_driver.py
│   ├── turn_runner.py
│   └── workplace.py
├── queue/                   # Layer 3 — 队列管理
│   └── user_input_queue.py
├── DAO/                     # Layer 3 — 数据访问（当前未使用）
│   └── messages.py
└── utils/                   # Layer 4 — 通用工具
    ├── atomic_io.py
    └── ws_helpers.py
```

## 现有 import 关系分析

通过 `rg "^from server\."` 扫描，当前跨子包 import 关系如下：

| 导出方 ↓ \ 导入方 →            | trigger    | service | queue | DAO | utils |
| ------------------------------ | ---------- | ------- | ----- | --- | ----- |
| **trigger.core**               | 内部       |         |       |     |       |
| **trigger.subagent_serialize** | 内部       |         |       |     |       |
| **trigger.http.helpers**       | 内部       |         |       |     |       |
| **trigger.ws.push_channel**    | 内部       |         |       |     |       |
| **service**                    | ✅         | 内部    |       |     |       |
| **queue**                      | ✅         | ✅      | —     |     |       |
| **DAO**                        | (无人引用) |         |       | —   |       |
| **utils**                      | ✅         | ✅      |       |     | —     |

> ✅ = 存在跨子包 import；空 = 无引用；内部 = 同层引用不违规

**现有 skip-level 引用（trigger 直接 import utils）：**

| 文件                       | 引用                      | 说明                |
| -------------------------- | ------------------------- | ------------------- |
| `trigger/http/channels.py` | `server.utils.atomic_io`  | 原子写文件          |
| `trigger/http/skills.py`   | `server.utils.atomic_io`  | 原子写文件          |
| `trigger/ws/messages.py`   | `server.utils.ws_helpers` | WebSocket JSON 发送 |

> import-linter `layers` 契约默认允许 skip-level（上层可引用任意下层），这些不违规。

## 层级契约定义

```
trigger   (Layer 1, 最上层)
  ↓ 可引用 ↓
service   (Layer 2)
  ↓ 可引用 ↓
queue, DAO (Layer 3, 同层互不引用)
  ↓ 可引用 ↓
utils     (Layer 4, 最底层)
```

- **trigger** → 可引用 service、queue、DAO、utils
- **service** → 可引用 queue、DAO、utils
- **queue / DAO** → 可引用 utils；**不可互相引用**（同层独立）
- **utils** → 不可引用 server 任何子包

## 配置文件

### `pyproject.toml` 追加

```toml
[tool.importlinter]
root_package = "server"

[[tool.importlinter.contracts]]
name = "Server 层级依赖契约"
type = "layers"
layers = [
    "server.trigger",
    "server.service",
    "server.queue, server.DAO",
    "server.utils",
]
```

> `layers` 契约规则：
>
> - 每层可引用下方任意层（允许 skip-level）
> - 每层不可引用上方任何层
> - 同一行逗号分隔的包（`queue, DAO`）视为同层，互相不可引用

### 违规示例

```
# ❌ service 反向引用 trigger
from server.trigger.http.channels import ChannelRouter
→ import-linter: Layer "server.service" is not allowed to import from layer "server.trigger"

# ❌ utils 反向引用 service
from server.service.memory import read_memory_files
→ import-linter: Layer "server.utils" is not allowed to import from layer "server.service"

# ❌ queue 引用 DAO（同层互引）
from server.DAO.messages import MessageDAO
→ import-linter: Layer "server.queue" is not allowed to import from layer "server.DAO"

# ✅ trigger 引用 service（允许）
from server.service import resume_agent
# ✅ service 引用 queue（允许）
from server.queue.user_input_queue import UserInputQueueStatus
# ✅ trigger 直接引用 utils（允许 skip-level）
from server.utils.atomic_io import atomic_write_text
```

## 集成方式

### 1. 安装依赖

`pyproject.toml` `[project]` optional-dependencies 或 dev 依赖：

```toml
[project.optional-dependencies]
dev = ["import-linter>=2.1"]
```

```bash
uv pip install import-linter
```

### 2. 创建 hook 脚本 `scripts/hooks/import-linter.sh`

```bash
#!/usr/bin/env bash
# Layer dependency contract check for pre-commit.
#
# Runs import-linter on the server package to enforce architectural
# layering rules. Uses lint-imports CLI from import-linter.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

exec lint-imports
```

### 3. 加入 pre-commit（`.pre-commit-config.yaml`）

```yaml
# ---- backend layer contract ---------------------------------
- repo: local
  hooks:
    - id: import-linter
      name: import-linter (server layer contract)
      entry: bash scripts/hooks/import-linter.sh
      language: system
      pass_filenames: false
      require_serial: true
      files: ^server/.*\.py$
```

### 4. 加入 CI（`ci.yml`）

在 `backend-test` job 的测试步骤之后加入：

```yaml
- name: Check server layer contract
  run: uv run lint-imports
```

## 涉及文件清单

| 操作 | 文件                                               |
| ---- | -------------------------------------------------- |
| 编辑 | `pyproject.toml`（加 `[tool.importlinter]` 配置）  |
| 新建 | `scripts/hooks/import-linter.sh`                   |
| 编辑 | `.pre-commit-config.yaml`（加 import-linter hook） |
| 编辑 | `.github/workflows/ci.yml`（加 lint-imports step） |

---

# 十二、注释腐化防治：死代码检测 + JSDoc 校验 + Python docstring

## 背景

代码注释腐化（comment rot）是指注释与代码实际行为脱节：函数改名后旧注释仍在描述旧行为、参数增减后 JSDoc 未同步、删除了函数但残留的注释引用无人清理。没有工具能做语义级"注释 vs 代码"对比，但可以通过**强制文档存在 + 签名绑定 + 死代码清除**三层策略间接防治：

1. **死代码检测** — 删除未使用代码，其附着的注释一并消失，消除腐化源头
2. **JSDoc 签名校验** — JSDoc 中声明的参数名/类型必须与函数签名一致，签名变更时 lint 报错
3. **Python docstring 规则** — 强制公开 API 必须有 docstring，签名变更时 ruff 提示缺失

## 现状分析

| 维度             | 后端 (Python)                                           | 前端 (TS/Vue)                   |
| ---------------- | ------------------------------------------------------- | ------------------------------- |
| 死代码检测       | ruff `F401`(未用 import)、`F841`(未用变量) — **已启用** | 无                              |
| 文档签名校验     | 无                                                      | 无                              |
| docstring 存在性 | 无强制（项目有 docstring 但非门禁）                     | 无强制（项目有 JSDoc 但非门禁） |
| docstring 用量   | server/ 20 个文件、agent/ 多处有 `"""`                  | 10+ 文件有 `/** */` JSDoc 块    |

## 方案一：死代码检测

### 后端（已覆盖，无需改动）

ruff `F` 系列已启用（`select = ["E4", "E7", "E9", "F", "UP", "S110"]`），包含：

| 规则   | 检测内容           | 状态      |
| ------ | ------------------ | --------- |
| `F401` | 未使用的 import    | ✅ 已启用 |
| `F811` | 重定义未使用的名称 | ✅ 已启用 |
| `F841` | 未使用的局部变量   | ✅ 已启用 |
| `F501` | f-string 无占位符  | ✅ 已启用 |
| `F821` | 未定义的名称       | ✅ 已启用 |

> 无需改动 `pyproject.toml`。

### 前端：引入 knip

**knip** 检测前端项目中的未使用 exports、未使用文件、未使用 dependencies。

#### 安装

`client/package.json` devDependencies 新增：

```json
"knip": "^5.0.0"
```

scripts 新增：

```json
"knip": "knip"
```

#### 配置 `client/knip.json`

```json
{
  "entry": ["app/app.vue", "nuxt.config.ts", "eslint.config.mjs"],
  "project": ["app/**/*.{ts,vue}"],
  "ignore": ["app/i18n/locales/**", ".nuxt/**", "dist/**", "src-tauri/**"],
  "ignoreExports": ["app/plugins/**", "app/directives/**"]
}
```

- `entry` — 入口文件，knip 从这些文件出发追踪依赖树
- `ignoreExports` — Nuxt 插件和指令通过全局注册（非显式 import），不被 knip 视为未使用

#### 加入 pre-commit

创建 `scripts/hooks/knip.sh`：

```bash
#!/usr/bin/env bash
# Dead code detection for the frontend.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../client"
exec pnpm exec knip --no-progress
```

`.pre-commit-config.yaml` 追加：

```yaml
# Phase 5: dead code detection — knip
- id: knip
  name: knip (unused exports/files)
  entry: bash scripts/hooks/knip.sh
  language: system
  pass_filenames: false
  require_serial: true
  files: ^client/.*\.(ts|tsx|vue|js|mjs)$
```

#### 加入 CI

`ci.yml` frontend-test job 追加：

```yaml
- name: Detect unused code (knip)
  run: pnpm knip
```

## 方案二：JSDoc 签名校验

引入 `eslint-plugin-jsdoc`，校验 JSDoc 中声明的参数名、参数类型、返回类型与实际函数签名一致。

### 安装

`client/package.json` devDependencies 新增：

```json
"eslint-plugin-jsdoc": "^51.0.0"
```

### 配置 `client/eslint.config.mjs` 追加

```js
import jsdoc from "eslint-plugin-jsdoc";

export default defineConfig([
  // ... existing configs ...
  {
    files: ["**/*.{ts,vue}"],
    plugins: { jsdoc },
    rules: {
      // 参数名必须与函数签名一致
      "jsdoc/check-param-names": "error",
      // 参数顺序必须与函数签名一致
      "jsdoc/check-param-names": "error",
      // @returns 类型必须与实际返回类型匹配
      "jsdoc/check-types": "warn",
      // 不允许描述不存在的参数
      "jsdoc/no-undefined-types": "error",
      // JSDoc 块必须有描述文本
      "jsdoc/require-description": "warn",
      // 有 JSDoc 的函数必须声明所有参数
      "jsdoc/require-param": "error",
      // 有 JSDoc 的函数必须声明返回类型（如果有返回值）
      "jsdoc/require-returns": "warn",
    },
  },
]);
```

### 校验效果

```ts
// ❌ jsdoc/check-param-names: JSDoc 声明 userId 但函数参数是 id
/**
 * @param userId The user ID
 */
function getUser(id: string) { ... }

// ❌ jsdoc/require-param: 函数有两个参数但 JSDoc 只声明了一个
/**
 * @param name The user name
 */
function createUser(name: string, email: string) { ... }

// ✅ JSDoc 与签名完全一致
/**
 * @param id The user ID
 * @returns The user object
 */
function getUser(id: string): User { ... }
```

> **关键：** 当函数签名增删参数时，JSDoc 校验会立即报错，迫使开发者同步更新注释。

### 不强制要求所有函数都有 JSDoc

不启用 `jsdoc/require-jsdoc`（否则每个小函数都要写 JSDoc，噪声过大）。只校验**已有 JSDoc 的函数**是否与签名一致。

## 方案三：Python docstring 规则

ruff 的 `D` 系列规则（源自 pydocstyle）强制 docstring 存在性与格式。

### 选择规则子集（避免全量噪声）

全量 `D` 有 50+ 条规则，大部分是格式微调。只启用与腐化防治直接相关的子集：

```toml
[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "UP", "S110", "D100", "D101", "D102", "D103", "D200"]

[tool.ruff.lint.pydocstyle]
convention = "google"
```

| 规则                    | 检测内容                   | 腐化防治意义                          |
| ----------------------- | -------------------------- | ------------------------------------- |
| `D100`                  | 公开模块必须有 docstring   | 模块用途变更时，docstring 需同步      |
| `D101`                  | 公开类必须有 docstring     | 类职责变更时，docstring 需同步        |
| `D102`                  | 公开方法必须有 docstring   | 方法签名/行为变更时，docstring 需同步 |
| `D103`                  | 公开函数必须有 docstring   | 函数签名变更时，docstring 需同步      |
| `D200`                  | docstring 第一行不能空行   | 格式统一，降低维护负担                |
| `convention = "google"` | 统一 Google 风格 docstring | 格式统一，减少风格争论                |

### 排除规则（避免噪声）

```toml
[tool.ruff.lint.per-file-ignores]
# __init__.py 通常只有 re-export，不需 docstring
"__init__.py" = ["D104"]
# 测试文件不强制 docstring
"tests/**" = ["D100", "D101", "D102", "D103"]
# Vendored 代码
"skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_*/**" = ["D100", "D101", "D102", "D103"]
```

### 不启用的规则（过于嘈杂）

| 规则        | 原因                                    |
| ----------- | --------------------------------------- |
| `D400`      | "首句必须以句号结尾" — 中英文混用难统一 |
| `D403`      | "首词首字母大写" — 中文不适用           |
| `D210`      | "首行不能空格开头" — 过于琐碎           |
| `D404-D414` | section 格式 — 团队尚未形成约定         |

> **策略：** 先上 `D100-D103`（存在性门禁），格式规则后续逐步加。存在性门禁是防治腐化的核心 — 有 docstring 才有"签名变更需同步"的基线。

## 三层协同

```
开发者修改函数签名（增删参数）
  │
  ├── Python: ruff D102/D103 检测 → docstring 存在 → 需手动更新
  ├── TS/Vue: eslint-plugin-jsdoc → check-param-names 报错 → JSDoc 参数名不匹配
  └── 死代码: 如果删除了函数 → knip/ruff F401 → 残留 export/import 报错
```

```
开发者删除整个模块
  │
  └── knip (前端) / ruff F401 (后端) → 残留引用报错 → 注释引用也被清除
```

## 涉及文件清单

| 操作 | 文件                                                   |
| ---- | ------------------------------------------------------ |
| 编辑 | `client/package.json`（加 knip + eslint-plugin-jsdoc） |
| 新建 | `client/knip.json`                                     |
| 新建 | `scripts/hooks/knip.sh`                                |
| 编辑 | `client/eslint.config.mjs`（加 jsdoc 规则）            |
| 编辑 | `pyproject.toml`（加 D 规则 + pydocstyle）             |
| 编辑 | `.pre-commit-config.yaml`（加 knip hook）              |
| 编辑 | `.github/workflows/ci.yml`（加 knip step）             |

---

# 十三、一键打包脚本 build.sh

## 背景

项目有三条独立构建线：

| 构建目标     | 工具链       | 产物                           | 当前入口                             |
| ------------ | ------------ | ------------------------------ | ------------------------------------ |
| Python 后端  | uv + Docker  | Docker 镜像（端口 8080）       | `docker build`                       |
| Nuxt4 前端   | pnpm + Nuxt  | `dist/` 静态文件               | `cd client && pnpm build`            |
| Tauri 桌面端 | pnpm + cargo | 各平台安装包（.msi/.dmg/.deb） | `cd client/src-tauri && cargo build` |

目前没有统一入口，开发者需要记住三条命令、三个目录。需要一个**一键打包脚本**，根据参数选择构建目标。

## 思路

### 格式选择

| 方案        | 优点                       | 缺点                                  | 决策   |
| ----------- | -------------------------- | ------------------------------------- | ------ |
| Shell 脚本  | 零依赖、与 `start.sh` 一致 | Windows 原生不兼容（需 Git Bash/WSL） | **选** |
| Makefile    | 标准化、增量构建           | Windows 无 make、语法不直观           | 不选   |
| justfile    | 语法简洁、跨平台           | 需安装 just（新依赖）                 | 不选   |
| Python 脚本 | 跨平台、类型安全           | 打包本身不应依赖 Python 环境          | 不选   |

**结论：Shell 脚本（`.sh`）**，与已有的 `start.sh` 保持一致。开发者已具备 Bash 环境（本地开发用 Git Bash / WSL，CI 用 Linux runner）。

### 文件名与位置

- **名称：** `build.sh`（与 `start.sh` 对称，语义清晰）
- **位置：** 项目根目录（与 `start.sh`、`Dockerfile` 同级）

### 命令设计

```bash
# 全量打包（后端镜像 + 前端静态 + 桌面端）
./build.sh all

# 仅后端 Docker 镜像
./build.sh backend [--tag sherry-agent:dev]

# 仅前端 Web 静态产物
./build.sh frontend

# 仅桌面端安装包
./build.sh desktop

# 查看帮助
./build.sh help
```

### 执行流程

```
./build.sh backend
  │
  ├── 检查 .env 存在（不打包进镜像，但 Dockerfile 需要知道路径）
  ├── docker build --tag sherry-agent:latest .
  └── 输出镜像信息 + 运行命令提示

./build.sh frontend
  │
  ├── cd client
  ├── 检查 node_modules（缺失则 pnpm install）
  ├── pnpm build → dist/
  └── 输出产物路径 + 预览命令提示

./build.sh desktop
  │
  ├── cd client/src-tauri
  ├── 检查 Rust toolchain（cargo --version）
  ├── tauri build（内部先跑 pnpm build → 再 cargo build → bundle）
  └── 输出安装包路径

./build.sh all
  │
  ├── ./build.sh backend
  ├── ./build.sh frontend
  └── ./build.sh desktop
```

### 脚本内容

```bash
#!/usr/bin/env bash
# One-click build script for EMA AI Agent.
#
# Usage:
#   ./build.sh all          # Build everything (backend + frontend + desktop)
#   ./build.sh backend      # Docker image only
#   ./build.sh frontend     # Nuxt static build only
#   ./build.sh desktop      # Tauri desktop installer only
#   ./build.sh help          # Show this message
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colors ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'
info()  { echo -e "${GREEN}[build]${NC} $1"; }
warn()  { echo -e "${YELLOW}[build]${NC} $1"; }
fail()  { echo -e "${RED}[build]${NC} $1" >&2; exit 1; }

# ── Targets ───────────────────────────────────────────────────────────

build_backend() {
  info "Building Docker image (backend)..."
  cd "$ROOT_DIR"

  [ -f .env ] || warn ".env not found — container will need --env-file at runtime"

  docker build -t sherry-agent:latest .
  info "Docker image: sherry-agent:latest"
  info "Run: docker run -p 8080:8080 --env-file .env sherry-agent:latest"
}

build_frontend() {
  info "Building Nuxt static frontend..."
  cd "$ROOT_DIR/client"

  if [ ! -d node_modules ]; then
    info "node_modules missing, running pnpm install..."
    pnpm install
  fi

  pnpm build
  info "Frontend output: client/dist/"
  info "Preview: cd client && pnpm preview"
}

build_desktop() {
  info "Building Tauri desktop installer..."
  cd "$ROOT_DIR/client"

  if [ ! -d node_modules ]; then
    info "node_modules missing, running pnpm install..."
    pnpm install
  fi

  command -v cargo >/dev/null 2>&1 || fail "Rust toolchain not found — install rustup first"

  pnpm tauri build
  info "Desktop installer: client/src-tauri/target/release/bundle/"
}

build_all() {
  build_backend
  echo ""
  build_frontend
  echo ""
  build_desktop
  info "All targets built successfully."
}

# ── CLI ───────────────────────────────────────────────────────────────

show_help() {
  cat <<EOF
EMA AI Agent — one-click build script.

Usage: ./build.sh <target> [options]

Targets:
  all        Build everything (backend Docker + frontend + desktop)
  backend    Build Docker image only (sherry-agent:latest)
  frontend   Build Nuxt static frontend only (dist/)
  desktop    Build Tauri desktop installer only (.msi/.dmg/.deb)
  help       Show this help message

Examples:
  ./build.sh backend
  ./build.sh all
EOF
}

case "${1:-help}" in
  all)      build_all ;;
  backend)  build_backend ;;
  frontend) build_frontend ;;
  desktop)  build_desktop ;;
  help|*)   show_help ;;
esac
```

### 关键设计决策

| 决策                      | 理由                                                              |
| ------------------------- | ----------------------------------------------------------------- |
| 脚本放根目录              | 与 `start.sh`、`Dockerfile` 同级，`./build.sh` 即可调用           |
| 不做增量缓存              | Docker 有 layer cache，pnpm 有 store cache，脚本不重复造轮子      |
| `desktop` 依赖 `frontend` | Tauri 内部调 `pnpm build`（`beforeBuildCommand`），脚本不重复构建 |
| `all` 按序执行            | backend → frontend → desktop，桌面端依赖前端产物                  |
| `set -euo pipefail`       | 任意步骤失败立即终止，不产出半成品                                |
| `cargo` 前置检查          | 桌面端构建需要 Rust 工具链，提前报错而非等待 Nuxt 构建后失败      |

## 涉及文件清单

| 操作 | 文件       |
| ---- | ---------- |
| 新建 | `build.sh` |

---

# 十四、Dependabot：前后端依赖自动更新

## 背景

项目有两条依赖线：

- **后端**：`pyproject.toml` + `uv.lock`（Python，uv 管理）
- **前端**：`client/package.json` + `pnpm-lock.yaml`（Node.js，pnpm 管理）

依赖长期不更新会累积安全漏洞和 breaking change 债务。Dependabot 是 GitHub 内置的依赖更新机器人，定期检查新版本并自动创建 PR，配合 CI 门禁确保更新不破坏构建。

## 思路

### Dependabot 生态位

| 方面        | 能力                                                        |
| ----------- | ----------------------------------------------------------- |
| 检测新版本  | 定期轮询 PyPI / npm registry，发现新版本即开 PR             |
| 安全漏洞    | 收到 GitHub Security Advisory 后立即开 PR（不限周期）       |
| 版本约束    | 遵守 `pyproject.toml` 的 version specifiers                 |
| 锁文件更新  | 自动更新 `uv.lock` / `pnpm-lock.yaml`                       |
| PR 自动验证 | PR 触发 CI（ruff / eslint / pytest / vitest），失败自动反馈 |
| 分组更新    | 可将多个小版本更新合并为一个 PR，减少 PR 噪声               |

### 配置策略

| 生态           | ecosystem        | 更新频率 | 分组策略                    |
| -------------- | ---------------- | -------- | --------------------------- |
| Python (uv)    | `uv`             | 每周     | 开发依赖与生产依赖分组      |
| npm (pnpm)     | `npm`            | 每周     | 生产依赖与开发依赖分组      |
| GitHub Actions | `github-actions` | 每周     | actions/checkout 等版本升级 |

> **注意：** Dependabot 原生不支持 `uv` ecosystem。有两种替代方案：
>
> 1. 用 `pip` ecosystem + `pyproject.toml`（Dependabot 能读 `[project.dependencies]`，但不更新 `uv.lock`）
> 2. 用 `uv` ecosystem（GitHub 2025 年已支持 `uv` ecosystem，需确认仓库 Settings → Dependabot 已启用）
>
> 推荐方案 2；若 `uv` ecosystem 不可用，退回方案 1 并在 CI 中加 `uv lock --check` 步骤。

## 配置文件

### `.github/dependabot.yml`

```yaml
version: 2

updates:
  # ── 后端 Python (uv) ───────────────────────────────────
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    groups:
      # 生产依赖合并一个 PR
      production-deps:
        patterns:
          - "langchain*"
          - "langgraph*"
          - "robyn"
          - "loguru"
          - "dotenv"
          - "aiosqlite"
          - "croniter"
          - "pillow"
          - "opencv-python-headless"
          - "instructor"
          - "json_repair"
          - "json5"
          - "gguf"
          - "pipmaster"
          - "snkv"
          - "websockets"
          - "websocket-client"
      # 开发依赖合并一个 PR
      dev-deps:
        dependency-type: "development"
    commit-message:
      prefix: "deps(backend)"
    labels:
      - "dependencies"
      - "backend"

  # ── 前端 npm (pnpm) ────────────────────────────────────
  - package-ecosystem: "npm"
    directory: "/client"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    groups:
      # Nuxt/Vue 生态合并一个 PR
      nuxt-ecosystem:
        patterns:
          - "nuxt"
          - "vue"
          - "vue-router"
          - "@nuxtjs/*"
          - "@pinia/*"
          - "pinia*"
      # PrimeVue/Tailwind 生态合并一个 PR
      ui-ecosystem:
        patterns:
          - "primevue"
          - "@primevue/*"
          - "tailwindcss"
          - "@tailwindcss/*"
      # 开发/测试工具合并一个 PR
      dev-tools:
        dependency-type: "development"
    commit-message:
      prefix: "deps(frontend)"
    labels:
      - "dependencies"
      - "frontend"

  # ── GitHub Actions 版本 ────────────────────────────────
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 3
    commit-message:
      prefix: "deps(ci)"
    labels:
      - "dependencies"
      - "ci"
```

### 分组策略说明

| 分组              | 包含                              | 合并理由                         |
| ----------------- | --------------------------------- | -------------------------------- |
| `production-deps` | langchain/robyn/loguru 等生产依赖 | 同属后端运行时，一起测试         |
| `dev-deps`        | pytest/basedpyright/pre-commit    | 开发工具升级风险低，合并减少噪声 |
| `nuxt-ecosystem`  | nuxt/vue/pinia/i18n               | Nuxt 生态版本耦合度高，一起升级  |
| `ui-ecosystem`    | primevue/tailwindcss              | UI 组件库版本同步升级            |
| `dev-tools`       | eslint/vitest/knip 等             | 前端开发工具，一起测试           |

### Dependabot PR 的 CI 验证流程

Dependabot 创建 PR 后自动触发 CI：

```
Dependabot PR opened
  │
  ├── backend-test job (ci.yml)
  │     ├── uv sync --frozen (如果 lock 不匹配则失败)
  │     ├── ruff check
  │     ├── basedpyright
  │     ├── pytest
  │     └── lint-imports (import-linter)
  │
  ├── frontend-test job (ci.yml)
  │     ├── pnpm install
  │     ├── eslint
  │     ├── vue-tsc
  │     ├── vitest (unit + integration)
  │     ├── knip
  │     └── dpdm
  │
  └── PR 可合并条件: 全绿 ✅
```

> **关键：** CI 全绿后才能 auto-merge（需要在仓库 Settings → General → Pull Requests 勾选 "Allow auto-merge"）。可以在 `dependabot.yml` 中加 `auto-merge: true` 但不推荐 — 依赖更新应人工 review 后合并。

### 不自动 merge 的理由

| 情况            | 风险                                            |
| --------------- | ----------------------------------------------- |
| langchain minor | API breaking change（langchain 1.x 频繁改 API） |
| robyn major     | 服务器框架升级，需手动测试 WebSocket            |
| nuxt minor      | Nuxt 配置格式变化，需手动验证 SSG 输出          |
| primevue major  | 组件 API 变化，需检查模板                       |

> 安全补丁（patch 级）可考虑配 GitHub Auto-merge，但 langchain/robyn/nuxt 等核心库即使是 patch 也建议人工确认。

## 涉及文件清单

| 操作 | 文件                     |
| ---- | ------------------------ |
| 新建 | `.github/dependabot.yml` |

---

