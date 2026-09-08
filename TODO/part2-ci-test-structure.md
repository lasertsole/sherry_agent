# 六、GitHub Actions：前后端测试并行运行

## 背景

原有 CI（`ci.yml`）只跑后端 Python 测试，前端 Vitest 套件（unit + integration）在 CI 中没有对应 job。push/PR 时前端代码变更不会被自动测试覆盖。

## 思路

在 `ci.yml` 中新增 `frontend-test` job，与后端 `test` job 并行运行。两个 job 无依赖关系，互不阻塞。

| Job             | 运行环境      | 工具链      | 跑什么                                      |
| --------------- | ------------- | ----------- | ------------------------------------------- |
| `test`          | ubuntu-latest | uv + Python | `tests/run_tests_split.py`（后端 hermetic） |
| `frontend-test` | ubuntu-latest | pnpm + Node | `pnpm test` + `pnpm test:integration`       |

前端测试分两步：

1. **Unit tests**（`vitest.config.ts`）：纯 `.ts` composable 逻辑，`happy-dom` 环境，不需要 SFC 编译
2. **Integration tests**（`vitest.integration.config.ts`）：`.vue` SFC 编译（`@vitejs/plugin-vue`），挂载真实组件，后端 mock

`pnpm install` 的 `postinstall` 脚本会自动执行 `nuxt prepare` 生成 `.nuxt/` 目录（vitest 的 alias 依赖此目录结构）。

## 实现

### `ci.yml` 新增 `frontend-test` job

```yaml
frontend-test:
  name: Frontend tests (Vitest)
  runs-on: ubuntu-latest
  timeout-minutes: 15
  defaults:
    run:
      working-directory: client
  steps:
    - uses: actions/checkout@v4

    - name: Install pnpm
      uses: pnpm/action-setup@v4
      with:
        version: 9

    - name: Setup Node.js
      uses: actions/setup-node@v4
      with:
        node-version: 22
        cache: pnpm
        cache-dependency-path: client/pnpm-lock.yaml

    - name: Install dependencies
      run: pnpm install --frozen-lockfile

    - name: Run unit tests
      run: pnpm test

    - name: Run integration tests
      run: pnpm test:integration
```

### 关键设计点

| 决策                             | 选择                    | 理由                                                             |
| -------------------------------- | ----------------------- | ---------------------------------------------------------------- |
| 并行 vs 串行                     | 并行（两个独立 job）    | 前后端无依赖，并行节省 CI 时间                                   |
| Node 版本                        | 22 (LTS)                | Nuxt 4 要求 Node 18+，22 为当前 LTS                              |
| pnpm 版本                        | 9                       | 与本地 `package.json` 兼容                                       |
| `--frozen-lockfile`              | 是                      | CI 中不允许修改 lockfile，确保可复现                             |
| `defaults.run.working-directory` | `client`                | 所有 `run` 步骤在 `client/` 下执行，无需每个 step 写 `cd client` |
| `cache-dependency-path`          | `client/pnpm-lock.yaml` | setup-node 的 pnpm 缓存需指向正确的 lockfile 路径                |

### 并行结构

```
push / PR to main
  │
  ├── Job 1: test (Backend)
  │     checkout → uv sync → tests/run_tests_split.py
  │
  └── Job 2: frontend-test (Frontend)
        checkout → pnpm install → pnpm test → pnpm test:integration
  │
  ▼
两个 job 都通过 → CI ✅
任一 job 失败 → CI ❌（阻塞 merge）
```

## 涉及文件清单

| 操作 | 文件                       |
| ---- | -------------------------- |
| 编辑 | `.github/workflows/ci.yml` |

---

# 七、Python 测试目录重构为集中镜像式

## 现状

当前 `tests/` 按测试类型分目录，与源码目录无对应关系：

```
tests/
├── unit/              ← 按类型（纯逻辑，mock）
│   ├── subagent/      ← 部分镜像 agent/tools/subagent/
│   ├── server/        ← 部分镜像 server/
│   ├── channels/      ← 部分镜像 channels/
│   ├── runtime/       ← 部分镜像 runtime/
│   └── test_*.py      ← 大量散落在 unit/ 根目录的松散文件
├── integration/       ← 按类型（多组件联动，mock 后端）
├── module/            ← 按类型（单模块，可能触达 DB）
├── system/            ← 按类型（全链路）
├── regression/        ← 回归测试
├── full/              ← 真实 LLM e2e
└── run_tests_split.py ← 双进程隔离 runner（GROUP A=unit, GROUP B=integration+system+module）
```

问题：

1. **找测试难** — 要测 `agent/tools/subagent/spawn/core.py`，需要猜它在 `tests/unit/subagent/test_spawn.py`
2. **松散文件多** — `tests/unit/` 根目录有 40+ 个 `test_*.py`，不知道对应哪个源码模块
3. **类型分目录割裂** — 同一模块的 unit / integration / module 测试分散在不同顶层目录
4. **`run_tests_split.py` 的拆分逻辑耦合目录结构** — Group A/B 按顶层目录划分，重命名/移动测试文件会影响分组

## 目标

改为**集中镜像式**：`tests/` 下按源码目录结构镜像排列，测试类型用 pytest marker 区分而非目录区分。

```
tests/
├── agent/
│   ├── middlewares/
│   │   ├── test_base.py
│   │   ├── test_context_engine_session_guard.py
│   │   └── test_tool_guardrails.py
│   ├── tools/
│   │   ├── pub_base/
│   │   │   └── test_skill_usage.py
│   │   ├── subagent/
│   │   │   ├── test_spawn.py
│   │   │   ├── test_kill.py
│   │   │   ├── test_announce.py
│   │   │   └── ...
│   │   └── test_memory.py
│   └── test_stream_repetition_guard_wrapper.py
├── bus/
│   └── test_type_bus.py
├── channels/
│   ├── test_consume_loop.py
│   └── test_manager_lifecycle.py
├── server/
│   ├── service/
│   │   ├── test_auto_turn.py
│   │   ├── test_messages.py
│   │   └── ...
│   └── trigger/
│       ├── test_channels.py
│       └── ...
├── models/
│   └── test_utils.py
├── runtime/
│   └── test_runtime_core.py
├── conftest.py
└── run_tests_split.py
```

## 设计要点

### 1. 测试类型用 marker 而非目录

```python
# tests/agent/tools/subagent/test_spawn.py
import pytest

@pytest.mark.unit
def test_spawn_basic():
    ...

@pytest.mark.integration
def test_spawn_full_lifecycle():
    ...
```

- `@pytest.mark.unit` — 纯逻辑，全 mock
- `@pytest.mark.integration` — 多组件联动，后端 mock
- `@pytest.mark.module` — 单模块，可能触达 DB
- `@pytest.mark.system` — 全链路
- `@pytest.mark.regression` — 回归
- `@pytest.mark.llm_e2e` — 真实 LLM（已有）

CI / 本地按 marker 选择跑哪些测试：

```bash
pytest tests/ -m unit              # 只跑 unit
pytest tests/ -m integration       # 只跑 integration
pytest tests/ -m "not llm_e2e"      # 排除真实 LLM（默认）
```

### 2. `run_tests_split.py` 改为按 marker 拆分

```python
# 改前：按目录拆分
GROUPS = [
    ("A", "unit", ["tests/unit"]),
    ("B", "integration+system+module", ["tests/integration", "tests/system", "tests/module"]),
]

# 改后：按 marker 拆分（结构不再耦合目录）
GROUPS = [
    ("A", "unit", ["tests/", "-m", "unit"]),
    ("B", "integration+module+system", ["tests/", "-m", "integration or module or system"]),
]
```

双进程隔离的原因不变（`conftest.py` 的 `sys.modules` stub 泄染），只是分组依据从目录改为 marker。

### 3. `conftest.py` 层级调整

当前每个类型目录有独立 `conftest.py`（`tests/unit/conftest.py`、`tests/integration/conftest.py` 等）。镜像式下，`conftest.py` 按源码模块就近放置：

```
tests/
├── conftest.py                          ← 全局 fixture
├── agent/
│   ├── conftest.py                      ← agent 模块级 fixture
│   └── tools/
│       └── subagent/
│           └── conftest.py              ← subagent 专用 stub（原 tests/unit/subagent/conftest.py）
```

### 4. `pyproject.toml` 注册 marker

```toml
[tool.pytest.ini_options]
addopts = "-m 'not llm_e2e'"
markers = [
    "unit: pure logic, fully mocked",
    "integration: multi-component, backend mocked",
    "module: single module, may touch DB",
    "system: full system",
    "regression: regression tests",
    "llm_e2e: real LLM API tests (costs tokens)",
]
```

## 迁移策略

分阶段迁移，每阶段保证 CI 绿：

| 阶段 | 内容                                                                           | 风险                |
| ---- | ------------------------------------------------------------------------------ | ------------------- |
| 1    | `pyproject.toml` 注册 marker，给现有测试加 `@pytest.mark.unit/integration/...` | 低，纯标注          |
| 2    | `run_tests_split.py` 改为按 marker 拆分，验证双进程隔离仍有效                  | 中，需回归全量      |
| 3    | 逐模块移动测试文件到镜像路径（一次一个源码模块）                               | 中，import 路径变化 |
| 4    | 清理旧目录，移除 `tests/unit/`、`tests/integration/` 等类型目录                | 低，空目录          |

## 涉及文件（预估）

| 操作 | 范围                                                                                                                               |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------- |
| 编辑 | `pyproject.toml`（注册 marker）                                                                                                    |
| 编辑 | `tests/run_tests_split.py`（marker 拆分）                                                                                          |
| 移动 | 全部 `tests/unit/**/*.py`、`tests/integration/**/*.py`、`tests/module/**/*.py`、`tests/system/**/*.py`、`tests/regression/**/*.py` |
| 移动 | 各级 `conftest.py` 到镜像路径                                                                                                      |
| 编辑 | `ci.yml`（确认 pytest 命令适配新结构）                                                                                             |

---

# 八、前端测试改为就近式

## 现状

前端测试目前分两种组织方式，混合存在：

| 组织方式              | 路径                                                                                                   | 文件数 | 说明                                             |
| --------------------- | ------------------------------------------------------------------------------------------------------ | ------ | ------------------------------------------------ |
| 就近式（已部分采用）  | `client/app/composables/__tests__/*.test.ts`                                                           | 27     | 测试文件放在源码同级 `__tests__/` 目录           |
| 就近式（零散）        | `client/app/pages/home/__tests__/`、`client/app/directives/__tests__/`、`client/app/common/__tests__/` | 3      | 同上，但覆盖面不足                               |
| 集中式（integration） | `client/tests/integration/*.integration.test.ts`                                                       | 6      | 集中在独立 `tests/integration/` 目录，与源码分离 |

问题：

1. **integration 测试找源码难** — `tests/integration/chatbox.integration.test.ts` 对应的是 `app/pages/home/components/ChatBox.vue`，路径无映射关系
2. **两套 vitest config 割裂** — unit 用 `vitest.config.ts`，integration 用 `vitest.integration.config.ts`，setup 和 stub 各写一套
3. **新建组件时容易遗忘测试** — 测试目录与源码目录分离，心智负担大

## 目标

全部测试就近放置在源码同级 `__tests__/` 目录中，消除 `client/tests/` 集中目录：

```
client/
├── app/
│   ├── composables/
│   │   ├── bridge.ts
│   │   ├── messages.ts
│   │   └── __tests__/
│   │       ├── bridge.test.ts              ← unit
│   │       ├── messages.test.ts            ← unit
│   │       └── bridge.integration.test.ts   ← integration（从 tests/integration/ 移入）
│   ├── pages/home/components/
│   │   ├── ChatBox.vue
│   │   └── __tests__/
│   │       ├── ChatBox.integration.test.ts  ← 从 tests/integration/chatbox.integration.test.ts 移入
│   │       └── messageItems.test.ts        ← 已存在
│   ├── components/chat/
│   │   ├── inputBox.vue
│   │   └── __tests__/
│   │       └── inputBox.integration.test.ts ← 从 tests/integration/inputbox.integration.test.ts 移入
│   └── ...
└── vitest.config.ts   ← 统一为单一 config
```

## 设计要点

### 1. 统一 vitest config

合并 `vitest.config.ts` + `vitest.integration.config.ts` 为单一配置：

```ts
// vitest.config.ts（合并后）
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "~": fileURLToPath(new URL("./app", import.meta.url)),
      "@": fileURLToPath(new URL("./app", import.meta.url)),
      "~~": fileURLToPath(new URL("./", import.meta.url)),
      "@@": fileURLToPath(new URL("./", import.meta.url)),
      "vue-i18n": fileURLToPath(
        new URL(
          "./app/composables/__tests__/stubs/vue-i18n.ts",
          import.meta.url,
        ),
      ),
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
    // 统一 include：就近放置的 *.test.ts + *.integration.test.ts
    include: ["app/**/*.{test,spec}.ts"],
    setupFiles: ["app/composables/__tests__/setup.ts"],
    env: { VITE_API_BACK_URL: "http://localhost:8080" },
    css: false,
    deps: {
      optimizer: { include: ["vue", "@vue/test-utils"] },
    },
  },
});
```

关键变化：

- `@vitejs/plugin-vue` 加入 plugins（原仅 integration config 有，现在统一）
- `include` 合并为 `app/**/*.{test,spec}.ts`，integration 测试也匹配（文件名 `.integration.test.ts` 仍匹配 `*.test.ts`）
- `css: false` 从 integration config 合入（原 unit config 无此项）
- `vue-i18n` stub 统一指向 `__tests__/stubs/`（原两套 config 指向不同 stub 文件）

### 2. 文件命名约定

| 测试类型    | 文件名格式                     | 示例                          |
| ----------- | ------------------------------ | ----------------------------- |
| Unit        | `{模块名}.test.ts`             | `bridge.test.ts`              |
| Integration | `{模块名}.integration.test.ts` | `ChatBox.integration.test.ts` |

通过文件名后缀区分类型，不通过目录区分。CI 可按文件名 pattern 选择跑哪些：

```bash
# 只跑 unit
pnpm vitest run app/**/__tests__/*.test.ts --exclude '**/*.integration.test.ts'
# 只跑 integration
pnpm vitest run app/**/*.integration.test.ts
# 全跑
pnpm test
```

### 3. `package.json` scripts 调整

```json
{
  "scripts": {
    "test": "vitest run",
    "test:unit": "vitest run --exclude '**/*.integration.test.ts'",
    "test:integration": "vitest run '**/*.integration.test.ts'",
    "test:watch": "vitest"
  }
}
```

- `test` 跑全部（原 `test` + `test:integration` 合并）
- `test:unit` 只跑 unit（排除 integration）
- `test:integration` 只跑 integration（按文件名 pattern）
- 删除 `test:integration:watch`（`test:watch` 已覆盖全量 watch）

### 4. CI `ci.yml` 适配

```yaml
- name: Run unit tests
  run: pnpm test:unit

- name: Run integration tests
  run: pnpm test:integration
```

无需改动 job 结构，只是 script 名变化。

### 5. Stub 文件统一

当前有两套 `vue-i18n` stub：

- `app/composables/__tests__/stubs/vue-i18n.ts`（unit 用）
- `tests/integration/stubs/vue-i18n.ts`（integration 用）

合并后统一使用前者，删除 `tests/integration/stubs/`。

## 迁移步骤

| 步骤 | 内容                                                                             | 风险                |
| ---- | -------------------------------------------------------------------------------- | ------------------- |
| 1    | 合并两个 vitest config 为单一 `vitest.config.ts`，本地验证全量通过               | 中，需回归全部测试  |
| 2    | 将 `tests/integration/*.integration.test.ts` 逐个移入对应源码目录的 `__tests__/` | 中，import 路径变化 |
| 3    | 删除 `tests/integration/` 目录及其 stub                                          | 低，空目录清理      |
| 4    | 更新 `package.json` scripts                                                      | 低                  |
| 5    | 更新 `ci.yml` 中的 script 名                                                     | 低                  |

## 涉及文件（预估）

| 操作         | 文件                                                                   |
| ------------ | ---------------------------------------------------------------------- |
| 编辑（合并） | `client/vitest.config.ts`                                              |
| 删除         | `client/vitest.integration.config.ts`                                  |
| 移动         | `client/tests/integration/*.integration.test.ts` → 各 `__tests__/`     |
| 删除         | `client/tests/integration/stubs/`、`client/tests/integration/setup.ts` |
| 编辑         | `client/package.json`（scripts）                                       |
| 编辑         | `.github/workflows/ci.yml`（script 名）                                |

---

# 九、前后端命名规范：通过文件名/函数名格式防止代码外溢

## 背景

现有的 pre-commit 边界守卫（第一条）通过文件后缀（`.css/.rs/.js/.ts/.mjs/.vue`）防止前端文件出现在 `client/` 之外。但这只能拦"文件类型外溢"，无法拦"代码风格外溢"：

- 后端目录下出现 `camelCase.ts`（前端风格文件名）— 后缀合规但命名不伦不类
- 前端目录下出现 `user_service.ts`（后端 snake_case 风格文件名）— 后缀合规但风格冲突
- Python 文件中函数用 `camelCase`（前端风格）— PEP8 不合规
- TypeScript 文件中函数用 `snake_case`（后端风格）— JS/TS 惯例不合规

## 目标

通过**文件命名格式 + 函数/类命名格式**两层约定，配合 pre-commit 自动拦截，让前后端代码风格天然隔离。

## 命名规范矩阵

| 层面       | 后端 (Python)      | 前端 (TS/Vue)                      | 说明                                                |
| ---------- | ------------------ | ---------------------------------- | --------------------------------------------------- |
| 文件名     | `snake_case.py`    | `kebab-case.ts` / `PascalCase.vue` | 后端 PEP8；Vue 组件 PascalCase，工具文件 kebab-case |
| 类名       | `PascalCase`       | `PascalCase`                       | 两者一致，靠语言本身约束                            |
| 函数/方法  | `snake_case`       | `camelCase`                        | 后端 PEP8；前端 JS/TS 惯例                          |
| 变量       | `snake_case`       | `camelCase`                        | 同上                                                |
| 常量       | `UPPER_SNAKE_CASE` | `UPPER_SNAKE_CASE`                 | 两者一致                                            |
| Vue 组件名 | —                  | `PascalCase`（`MultiWord`）        | `vue/multi-word-component-names`                    |

## 实现方案

### 第一层：文件名格式检查（pre-commit 脚本）

新增 `scripts/hooks/check-naming-convention.sh`，对提交的文件名做格式校验：

```bash
#!/usr/bin/env bash
# File naming convention guard for pre-commit.
#
# Backend (.py):     snake_case.py      → ok
# Frontend (.ts):    kebab-case.ts      → ok
# Frontend (.vue):   PascalCase.vue     → ok
# Frontend (.scss):  kebab-case.scss    → ok
#
# Violations (examples):
#   server/UserService.py     → PascalCase in .py → ❌
#   client/app/my_module.ts   → snake_case in .ts → ❌
#   client/app/user.vue       → single lowercase word in .vue → ❌ (should be PascalCase)
set -euo pipefail

violations=()

for f in "$@"; do
  basename=$(basename "$f")
  name="${basename%.*}"
  ext="${basename##*.}"

  case "$ext" in
    py)
      # Python: snake_case (lowercase + digits + underscores)
      if ! [[ "$name" =~ ^[a-z_][a-z0-9_]*$ ]]; then
        violations+=("$f → .py must be snake_case")
      fi
      ;;
    ts|tsx|js|jsx|mjs|cjs)
      # TypeScript/JavaScript: kebab-case (lowercase + digits + hyphens)
      # Exception: .config.* and *.d.ts are allowed any case
      if [[ "$name" == *.d ]]; then continue; fi
      if [[ "$name" == *.config ]]; then continue; fi
      if ! [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]]; then
        violations+=("$f → .$ext must be kebab-case")
      fi
      ;;
    vue)
      # Vue SFC: PascalCase (at least two capitalized words, or single capitalized word)
      if ! [[ "$name" =~ ^[A-Z][a-zA-Z0-9]+$ ]]; then
        violations+=("$f → .vue must be PascalCase")
      fi
      ;;
    scss|css)
      # Style files: kebab-case
      if ! [[ "$name" =~ ^[a-z][a-z0-9-]*$ ]]; then
        violations+=("$f → .$ext must be kebab-case")
      fi
      ;;
  esac
done

if [ ${#violations[@]} -gt 0 ]; then
  echo "ERROR: file naming convention violations:" >&2
  printf '  %s\n' "${violations[@]}" >&2
  echo "" >&2
  echo "Naming rules:" >&2
  echo "  .py  → snake_case   (e.g. user_service.py)" >&2
  echo "  .ts  → kebab-case   (e.g. use-theme.ts)" >&2
  echo "  .vue → PascalCase   (e.g. ChatBox.vue)" >&2
  echo "  .scss→ kebab-case   (e.g. main.scss)" >&2
  exit 1
fi
```

### 第二层：代码内命名检查（lint 规则）

#### 后端：ruff 加 `pep8-naming`（N 系列规则）

`pyproject.toml`：

```toml
[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "UP", "S110", "N"]

[tool.ruff.lint.pep8-naming]
# 允许 Django 风格的 setUp/tearDown 等测试方法
extend-ignore-names = ["setUp", "tearDown", "setUpClass", "tearDownClass"]
```

主要规则：

| 规则   | 检查内容                   | 示例                                              |
| ------ | -------------------------- | ------------------------------------------------- |
| `N801` | 类名必须 PascalCase        | `class UserService:` ✅ / `class userService:` ❌ |
| `N802` | 函数名必须 snake_case      | `def get_user():` ✅ / `def getUser():` ❌        |
| `N803` | 参数名必须 snake_case      | `def fn(user_id):` ✅ / `def fn(userId):` ❌      |
| `N806` | 模块级变量必须 UPPER_SNAKE | `MAX_RETRIES = 3` ✅ / `maxRetries = 3` ❌        |
| `N811` | 类型别名导入不重命名为小写 | —                                                 |

#### 前端：ESLint 加 `@typescript-eslint/naming-convention`

`client/eslint.config.mjs`：

```js
{
  files: ['**/*.{ts,tsx,vue}'],
  rules: {
    '@typescript-eslint/naming-convention': ['error', {
      // 函数/变量：camelCase
      selector: 'default',
      format: ['camelCase'],
      leadingUnderscore: 'allow',
      trailingUnderscore: 'allow',
    }, {
      // 类名：PascalCase
      selector: 'class',
      format: ['PascalCase'],
    }, {
      // 常量：UPPER_SNAKE_CASE
      selector: 'variable',
      modifiers: ['const'],
      format: ['camelCase', 'UPPER_SNAKE_CASE'],
    }, {
      // 类型/接口：PascalCase
      selector: ['typeLike', 'interface'],
      format: ['PascalCase'],
    }],
  },
},
```

### 第三层：pre-commit 编排

```yaml
# ---- naming convention guard (frontend + backend file names) ------------
- repo: local
  hooks:
    - id: naming-convention
      name: file naming convention
      entry: bash scripts/hooks/check-naming-convention.sh
      language: system
      files: \.(py|ts|tsx|js|jsx|mjs|cjs|vue|scss|css)$
      exclude: ^client/node_modules/
```

## 效果

```
# ❌ 后端目录出现前端风格文件名
git add server/UserService.py    → naming-convention hook 拦截（.py 必须 snake_case）
git add agent/getUserData.ts     → frontend-file-boundary hook 拦截（.ts 不在 client/）

# ❌ 前端目录出现后端风格文件名
git add client/app/user_service.ts → naming-convention hook 拦截（.ts 必须 kebab-case）
git add client/app/chat_box.vue     → naming-convention hook 拦截（.vue 必须 PascalCase）

# ❌ 后端代码内函数用 camelCase
def getUserData():              → ruff N802 拦截

# ❌ 前端代码内函数用 snake_case
function get_user_data()        → eslint naming-convention 拦截
```

## 涉及文件（预估）

| 操作 | 文件                                                    |
| ---- | ------------------------------------------------------- |
| 新建 | `scripts/hooks/check-naming-convention.sh`              |
| 编辑 | `.pre-commit-config.yaml`（加 naming-convention hook）  |
| 编辑 | `pyproject.toml`（ruff select 加 `N`）                  |
| 编辑 | `client/eslint.config.mjs`（加 naming-convention 规则） |
| 编辑 | `client/package.json`（确认无冲突）                     |

---

# 十、DPDM：前端循环依赖检测

## 背景

前端项目（Nuxt4 + TypeScript）随规模增长，模块间 import 关系日趋复杂。循环引用（A → B → C → A）不会立即报错，但会导致初始化顺序不确定、tree-shaking 失效、运行时 `undefined` 等隐蔽 bug。需要引入静态检测工具在提交阶段和 CI 阶段自动拦截。

## 思路

**dpdm**（Detect Pre-existing Dependency Mess）是一个 JS/TS 循环依赖检测工具。它从入口文件出发静态追踪 import 链，输出所有环形引用路径。

- 入口文件：`app/app.vue`（Nuxt 应用的根组件，所有页面/组件的 import 树起点）
- `--circular`：只输出循环依赖，忽略普通依赖树
- `--warning`：发现循环依赖时以非零退出码退出（用于 pre-commit / CI 门禁）

## 实际操作

### 1. 安装依赖

`client/package.json` devDependencies 新增：

```json
"dpdm": "^3.14.0"
```

scripts 新增：

```json
"dpdm": "dpdm app/app.vue --circular --warning"
```

### 2. 创建 hook 脚本 `scripts/hooks/dpdm.sh`

```bash
#!/usr/bin/env bash
# Circular dependency detection gate for pre-commit.
#
# Runs dpdm on the Nuxt app entry point (app/app.vue) to detect circular
# import chains. The --circular flag limits output to cycles only;
# --warning makes dpdm exit non-zero when cycles are found.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

exec pnpm exec dpdm app/app.vue --circular --warning
```

### 3. 加入 pre-commit（`.pre-commit-config.yaml`）

作为前端 fix-then-check 流程的 Phase 4：

```yaml
# Phase 4: circular dependency detection — dpdm on app entry
- id: dpdm
  name: dpdm (circular deps)
  entry: bash scripts/hooks/dpdm.sh
  language: system
  pass_filenames: false
  require_serial: true
  files: ^client/.*\.(ts|tsx|vue|js|mjs)$
```

- `pass_filenames: false` — dpdm 始终分析整个 import 树，不接受单文件参数
- `files` 限定前端文件类型变更时才触发

### 4. 加入 CI（`ci.yml`）

在 `frontend-test` job 的测试步骤之后加入：

```yaml
- name: Detect circular dependencies
  run: pnpm dpdm
```

### 5. 完整前端 hook 流程

pre-commit 时（四阶段）：

```
Phase 1: frontend-fix    — prettier --write + eslint --fix
Phase 2: eslint          — check-only
Phase 3: vue-tsc         — 类型检查
Phase 4: dpdm            — 循环依赖检测
```

CI 时（frontend-test job）：

```
pnpm install
  → pnpm test          (Vitest unit)
  → pnpm test:integration (Vitest integration)
  → pnpm dpdm          (循环依赖检测)
```

## 效果

```
# ❌ 存在循环依赖时
$ pnpm dpdm
Circular dependencies:
  app/composables/bridge.ts → app/composables/messages.ts → app/composables/bridge.ts

Process exited with code 1
→ pre-commit 拦截 / CI 失败

# ✅ 无循环依赖
$ pnpm dpdm
No circular dependencies found.
Process exited with code 0
→ 放行
```

## 涉及文件清单

| 操作 | 文件                                       |
| ---- | ------------------------------------------ |
| 编辑 | `client/package.json`（devDep + script）   |
| 新建 | `scripts/hooks/dpdm.sh`                    |
| 编辑 | `.pre-commit-config.yaml`（加 dpdm hook）  |
| 编辑 | `.github/workflows/ci.yml`（加 dpdm step） |

---

