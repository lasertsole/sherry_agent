# 开发规范与配置变更记录

| #    | 分类     | 标题                                                                                                                   |
| ---- | -------- | ---------------------------------------------------------------------------------------------------------------------- |
| 一   | 项目结构 | [Pre-commit 边界守卫：前端/Rust 文件限制在 client/ 目录](#一pre-commit-边界守卫前端rust-文件限制在-client-目录)        |
| 二   | 后端质量 | [Ruff S110 规则：禁止 try-except-pass 静默吞异常](#二ruff-s110-规则禁止-try-except-pass-静默吞异常)                    |
| 三   | 前端功能 | [Nuxt4 自定义指令全局注册：v-debounce / v-safe-html 免引入](#三nuxt4-自定义指令全局注册v-debounce--v-safe-html-免引入) |
| 四   | 前端质量 | [ESLint + Prettier 配置与 Pre-commit fix-then-check 流程](#四eslint--prettier-配置与-pre-commit-fix-then-check-流程)   |
| 五   | 提交规范 | [Commitlint：提交信息强制 Angular 风格](#五commitlint提交信息强制-angular-风格)                                        |
| 六   | CI       | [GitHub Actions：前后端测试并行运行](#六github-actions前后端测试并行运行)                                              |
| 七   | 测试结构 | [Python 测试目录重构为集中镜像式](#七python-测试目录重构为集中镜像式)                                                  |
| 八   | 测试结构 | [前端测试改为就近式](#八前端测试改为就近式)                                                                            |
| 九   | 代码规范 | [前后端命名规范：通过文件名/函数名格式防止代码外溢](#九前后端命名规范通过文件名函数名格式防止代码外溢)                 |
| 十   | 代码质量 | [DPDM：前端循环依赖检测](#十dpdm前端循环依赖检测)                                                                      |
| 十一 | 代码质量 | [import-linter：server 层级依赖契约](#十一import-linterserver-层级依赖契约)                                            |
| 十二 | 代码质量 | [注释腐化防治：死代码检测 + JSDoc 校验 + Python docstring](#十二注释腐化防治死代码检测--jsdoc-校验--python-docstring)  |
| 十三 | 构建部署 | [一键打包脚本 build.sh](#十三一键打包脚本-buildsh)                                                                     |
| 十四 | 依赖管理 | [Dependabot：前后端依赖自动更新](#十四dependabot前后端依赖自动更新)                                                    |
| 十五 | 代码质量 | [Pre-push 门禁：重操作从 pre-commit 分离](#十五pre-push-门禁重操作从-pre-commit-分离)                                  |
| 十六 | 代码质量 | [Ruff T20：禁用 print，强制使用 loguru](#十六ruff-t20禁用-print强制使用-loguru)                                        |
| 十七 | 代码质量 | [ESLint no-console：禁用 console，强制使用日志工具](#十七eslint-no-console禁用-console强制使用日志工具)                |
| 十八 | 安全     | [沙箱防逃逸与子代理权限递减](#十八沙箱防逃逸与子代理权限递减)                                                          |

---

# 一、Pre-commit 边界守卫：前端/Rust 文件限制在 client/ 目录

## 背景

项目根目录下有多个后端目录（`agent/`、`bus/`、`channels/`、`server/` 等），前端及 Rust 代码统一放在 `client/`（其中 Rust 代码在 `client/src-tauri/`）。需要防止前端文件类型意外"外溢"到后端目录。

**目标：** 如果提交中包含 `client/` 之外的 `.css`、`.rs`、`.js`、`.ts`、`.mjs`、`.vue` 文件，pre-commit 直接拒绝提交。

## 实现方案

利用 pre-commit 的 `files` + `exclude` 过滤器做一个"金丝雀"（canary）hook：该 hook **只在出现违规文件时才触发**，触发即失败。

### 核心原理

pre-commit 对每个 hook 有两个过滤器：

| 过滤器    | 作用                                |
| --------- | ----------------------------------- |
| `files`   | 正则，只有文件路径匹配的才传给 hook |
| `exclude` | 正则，匹配的文件从候选列表中移除    |

两者**同时生效**，交集为最终传给 hook 的文件列表。当交集为空时，pre-commit **跳过该 hook**（不调用 `entry` 命令）。

因此可以构造一个"反向"hook：

```
files:   \.(css|rs|js|ts|mjs|vue)$   ← 匹配目标后缀
exclude: ^client/                     ← 排除 client/ 下的文件
```

- 文件在 `client/` 内 → 被 `exclude` 移除 → hook 不触发 → 放行
- 文件在 `client/` 外且后缀匹配 → 不被排除 → 传入 hook → **失败**
- 文件后缀不匹配 → `files` 不命中 → hook 不触发 → 放行

### 涉及文件

#### 1. `scripts/hooks/reject-stray-frontend.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "ERROR: file types css/rs/js/ts/mjs/vue must live under client/ — stray files:" >&2
printf '  %s\n' "$@" >&2
echo "Hint: move them into client/ or remove them." >&2
exit 1
```

脚本逻辑极简：pre-commit 把违规文件路径作为参数传入（`$@`），脚本只需打印并 `exit 1`。收到任何参数即代表有违规文件，必定失败。

#### 2. `.pre-commit-config.yaml` 中的 hook 定义

```yaml
# ---- boundary guard: frontend/Rust files must stay inside client/ --------
- repo: local
  hooks:
    - id: frontend-file-boundary
      name: enforce css/rs/js/ts/mjs/vue in client/
      entry: bash scripts/hooks/reject-stray-frontend.sh
      language: system
      files: \.(css|rs|js|ts|mjs|vue)$
      exclude: ^client/
```

| 字段       | 值                                            | 说明                             |
| ---------- | --------------------------------------------- | -------------------------------- |
| `repo`     | `local`                                       | 本地 hook，不需要外部仓库        |
| `language` | `system`                                      | 直接调用系统命令，不需要安装环境 |
| `entry`    | `bash scripts/hooks/reject-stray-frontend.sh` | 调用拒绝脚本                     |
| `files`    | `\.(css\|rs\|js\|ts\|mjs\|vue)$`              | 正则匹配目标后缀                 |
| `exclude`  | `^client/`                                    | 排除 client/ 下所有文件          |

## 各场景行为表

| 提交的文件                     | `files` 匹配 | `exclude` 移除 | hook 触发 | 结果     |
| ------------------------------ | :----------: | :------------: | :-------: | -------- |
| `server/foo.ts`                |      是      |       否       |    是     | **拒绝** |
| `agent/bar.rs`                 |      是      |       否       |    是     | **拒绝** |
| `scripts/baz.js`               |      是      |       否       |    是     | **拒绝** |
| `client/src/foo.ts`            |      是      |       是       |    否     | 放行     |
| `client/src-tauri/src/main.rs` |      是      |       是       |    否     | 放行     |
| `server/main.py`               |      否      |       —        |    否     | 放行     |
| `config/settings.json`         |      否      |       —        |    否     | 放行     |
| （无匹配文件）                 |      —       |       —        |    否     | 放行     |

## 为什么不用 `types_or`

pre-commit 的 `types` / `types_or` 基于 [identify](https://github.com/pre-commit/identify) 库的文件类型识别。虽然它支持 `javascript`、`ts`、`vue`、`css`、`rust` 等类型，但：

1. `javascript` 类型会同时覆盖 `.js`、`.mjs`、`.cjs`，无法单独控制
2. 类型系统不够直观，无法精确列出"只允许这 6 种后缀"
3. 用 `files` 正则更直观、更易维护，后缀列表一目了然

因此选择 `files` 正则方案。

## 扩展与维护

### 新增受限后缀

编辑 `.pre-commit-config.yaml` 中的 `files` 正则，在 `|` 分隔的列表中添加：

```yaml
files: \.(css|rs|js|ts|mjs|vue|svelte|astro)$
```

### 新增豁免目录

如果将来有第二个前端目录（如 `admin/`），修改 `exclude`：

```yaml
exclude: ^(client|admin)/
```

### 临时绕过

pre-commit 支持 `--no-verify` 标志跳过所有 hook（应仅在紧急情况下使用）：

```bash
git commit --no-verify -m "emergency hotfix"
```

## 与现有 hook 的关系

| hook                             | 作用域                         | 做什么                       |
| -------------------------------- | ------------------------------ | ---------------------------- |
| `ruff` / `ruff-format`           | Python 文件                    | 检查后端代码风格             |
| `basedpyright`                   | Python 文件                    | 后端类型检查                 |
| `pretter` / `eslint` / `vue-tsc` | `client/` 内的前端文件         | 检查前端代码风格与类型       |
| **`frontend-file-boundary`**     | `client/` **外**的目标后缀文件 | **拦截违规文件，不允许提交** |

前三组 hook 是"在正确位置做正确检查"，边界守卫 hook 是"确保文件在正确位置"——两者互补。

## 附：basedpyright 移除 exclude

原本 `basedpyright` hook 有一个 `exclude` 字段跳过 `tests/`、`agent/`、`models/` 等目录（对应 pyproject 中的类型债务清单）：

```yaml
exclude: ^(tests|agent|models|skills|plugins|channels|runtime|docs)/
```

现已移除该行，`basedpyright` 会对**所有** Python 文件运行类型检查，不再有豁免区域。这要求上述目录的类型标注逐步补全，但能防止未类型化的代码在提交时"静默通过"。

---

# 二、Ruff S110 规则：禁止 try-except-pass 静默吞异常

## 背景

代码库中存在大量 `try: ... except: pass` 模式（约 45 处非 vendored 代码），异常被静默吞掉，可能隐藏 bug。需要让 ruff 在 lint 和 pre-commit 阶段自动检出此类模式，同时妥善处理存量代码。

## 思路

### 规则选择

ruff 的 `S110`（源自 flake8-bandit）专门检测 `try-except-pass` 模式：当 `except` 块的 body 只有 `pass` 时报错。该规则**无自动 fix**，仅诊断，适合做门禁。

### 存量处理策略

| 代码类型              | 处理方式                    | 理由                                          |
| --------------------- | --------------------------- | --------------------------------------------- |
| Vendored 代码         | `per-file-ignores` 整体豁免 | 第三方代码，不修改上游风格                    |
| Skill 脚本            | `per-file-ignores` 整体豁免 | 脚本常用 try-except-pass 做最佳努力清理       |
| FunASR vendored utils | `per-file-ignores` 整体豁免 | 已有 `E722` 豁免，同属 vendored               |
| 项目自有代码          | 逐行添加 `# noqa: S110`     | 显式标注每一处，后续可逐个清理为 log/re-raise |

### Pre-commit 集成

`.pre-commit-config.yaml` 已有 `ruff` hook（`ruff-pre-commit` repo），hook 默认读取 `pyproject.toml` 中的 `[tool.ruff.lint]` 配置。只需在 `select` 列表加入 `"S110"` 即可，无需修改 `.pre-commit-config.yaml`。

## 实际操作

### 1. 修改 `pyproject.toml`：加入 S110 规则

```toml
[tool.ruff.lint]
# S110 (try-except-pass) from flake8-bandit: flags silent exception swallowing;
# use # noqa: S110 for intentional cases (ImportError, OSError cleanup, asyncio.CancelledError).
select = ["E4", "E7", "E9", "F", "UP", "S110"]
```

### 2. 修改 `pyproject.toml`：扩展 per-file-ignores

```toml
[tool.ruff.lint.per-file-ignores]
# Vendored LightRAG / RAG-Anything code
"skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_raganything/*" = ["E402", "F403", "F405", "S110"]
"skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag/**" = ["S110"]
# Skill scripts: intentional delayed imports + best-effort cleanup
"skills/builtin/**/scripts/*" = ["E402", "S110"]
# Vendored FunASR onnxruntime utils
"models/STT_model/utils/*" = ["E722", "S110"]
```

### 3. 给 45 处存量 try-except-pass 添加 `# noqa: S110`

通过脚本批量在 `except` 行末尾追加 `# noqa: S110`，覆盖以下目录：

| 目录       | 文件数 | noqa 数 |
| ---------- | ------ | ------- |
| `agent/`   | 16     | 30      |
| `server/`  | 8      | 9       |
| `plugins/` | 1      | 1       |
| `models/`  | 4      | 5       |
| **合计**   | 29     | 45      |

完整文件清单：

```
agent/tools/pub_base/skill_usage.py          (3处)
agent/tools/skill_tools/skill_manage.py     (3处)
agent/tools/skill_tools/skill_view.py        (1处)
agent/tools/subagent/announce/core.py        (1处)
agent/tools/subagent/announce/steering_queue.py (1处)
agent/tools/subagent/control/kill.py         (2处)
agent/tools/subagent/control/send.py         (1处)
agent/tools/subagent/followup/core.py        (1处)
agent/tools/subagent/hooks/base.py           (2处)
agent/tools/subagent/registry/lifecycle.py   (1处)
agent/tools/subagent/registry/sweeper.py    (1处)
agent/tools/subagent/spawn/core.py           (5处)
agent/tools/subagent/swarm/collector.py      (1处)
agent/tools/memory.py                        (3处)
agent/stream_repetition_guard_wrapper.py     (4处)
server/service/auto_turn.py                  (1处)
server/service/messages.py                   (1处)
server/service/skill_scanner.py              (1处)
server/service/stream_dispatch.py            (1处)
server/trigger/channels/core.py              (1处)
server/trigger/http/channels.py              (2处)
server/trigger/http/knowledge_graph.py       (1处)
server/trigger/ws/messages.py                (1处)
plugins/channels/qq/core.py                  (1处)
models/extract_model/core.py                 (1处)
models/LLMs/auxiliary_llm/core.py            (1处)
models/reranker_model/core.py                (2处)
models/utils.py                              (1处)
```

### 4. 验证

- 重新扫描 `agent/`、`server/`、`plugins/`、`models/` 四个目录：**0 处未标注的 try-except-pass**
- pre-commit 的 `ruff` hook 无需修改，自动读取 `pyproject.toml` 配置
- 新代码中出现未标注的 `try-except-pass` 将在 `git commit` 时被 ruff 拦截

## 规则效果

```python
# ❌ 会被 ruff S110 拦截（pre-commit 阶段报错）
try:
    do_something()
except Exception:
    pass

# ✅ 推荐做法：用 loguru 记录异常，不要静默吞掉
from loguru import logger

try:
    do_something()
except Exception as e:
    logger.error(f"do_something failed: {e}")

# ✅ 合法：显式标注（仅限 ImportError / OSError 清理 / asyncio.CancelledError 等确需静默的场景）
try:
    do_something()
except Exception:  # noqa: S110
    pass
```

> **规范：** 新代码中 `try-except-pass` 默认使用 `from loguru import logger` 记录异常日志，不得直接 `pass`。仅在 `ImportError`（可选依赖）、`OSError`（清理已删文件）、`asyncio.CancelledError`（任务取消）等确需静默的场景才使用 `# noqa: S110`。存量 45 处 `# noqa: S110` 应逐步替换为 logger 记录。

## 涉及文件清单

| 操作                              | 文件                                        |
| --------------------------------- | ------------------------------------------- |
| 编辑（select + per-file-ignores） | `pyproject.toml`                            |
| 编辑（追加 `# noqa: S110`）       | `agent/` 下 16 个文件                       |
| 编辑（追加 `# noqa: S110`）       | `server/` 下 8 个文件                       |
| 编辑（追加 `# noqa: S110`）       | `plugins/channels/qq/core.py`               |
| 编辑（追加 `# noqa: S110`）       | `models/` 下 4 个文件                       |
| 无需修改                          | `.pre-commit-config.yaml`（已有 ruff hook） |

---

# 三、Nuxt4 自定义指令全局注册：v-debounce / v-safe-html 免引入

## 背景

项目根目录 `src/directives/debounce.ts` 存在一个旧版防抖指令实现。与此同时 `client/app/directives/debounce.ts` 已有一份更精简的实现（用 Symbol 存储 state、stable listener 避免重复 bind/unbind）。新版实现优于旧版，故删除旧版，后续统一在新版基础上开展工作。但各 Vue 文件仍需手动 `import { vDebounce } from '~/directives/debounce'`，使用繁琐且容易遗漏。

**目标：** 将指令逻辑统一到 `client/app/` 下，通过 Nuxt 插件全局注册，Vue 模板中直接写 `v-debounce` / `v-safe-html` 即可，无需任何显式 import。

## 思路

Nuxt 的 `imports.dirs` 配置可以将目录的导出自动注入 `<script setup>`，但 Vue 指令需要在 Vue 应用层面通过 `app.directive()` 注册才能在模板中作为 `v-xxx` 使用。仅靠 `imports.dirs` 自动导入虽然能在 `<script setup>` 拿到 `vDebounce` 变量（Vue 编译器会将 `v` 开头大写驼峰变量识别为指令），但仍需要每个文件"隐式引用"该变量，不够干净。

**最终方案：** 创建一个 Nuxt 插件，在插件中调用 `nuxtApp.vueApp.directive('debounce', vDebounce)` 做全局注册。Nuxt 插件在应用启动时自动执行，所有组件模板均可直接使用 `v-debounce`，零 import。

## 实际操作

### 1. 创建 Nuxt 插件 `client/app/plugins/directives.ts`

```ts
import { vDebounce } from "~/directives/debounce";
import { vSafeHtml } from "~/directives/safeHtml";

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.directive("debounce", vDebounce);
  nuxtApp.vueApp.directive("safe-html", vSafeHtml);
});
```

Nuxt 会自动扫描 `app/plugins/` 目录并在启动时执行插件，无需在 `nuxt.config.ts` 中手动注册。

### 2. 移除所有 Vue 文件中的手动 import

以下 6 个文件去掉了 `import { vDebounce } from '~/directives/debounce'` 或 `import { vSafeHtml } from '@/directives/safeHtml'`：

| 文件                                         | 移除的 import                                       |
| -------------------------------------------- | --------------------------------------------------- |
| `app/pages/home/components/StatsDialog.vue`  | `import { vDebounce } from '~/directives/debounce'` |
| `app/pages/home/components/SkillsDialog.vue` | 同上                                                |
| `app/pages/home/components/LogsDialog.vue`   | 同上                                                |
| `app/pages/home/components/CronDialog.vue`   | 同上                                                |
| `app/components/chat/inputBox.vue`           | 同上                                                |
| `app/pages/home/components/ChatBox.vue`      | `import { vSafeHtml } from '@/directives/safeHtml'` |

模板中的 `v-debounce:click.500="..."` 和 `v-safe-html="..."` 保持不变，由插件全局注册后自动生效。

### 3. 删除旧版实现

旧版 `src/directives/debounce.ts` 实现不如新版（新版用 Symbol 存储 state、stable listener 避免重复 bind/unbind），直接删除，后续基于新版工作。

- 删除 `src/directives/debounce.ts`（旧实现，无外部引用）
- 删除空目录 `src/directives/`

`client/app/directives/debounce.ts` 保留为唯一实现。

### 4. 验证

- `grep` 确认所有 `.vue` 文件中不再有 `import.*vDebounce` / `import.*vSafeHtml` 拋留
- LSP 诊断扫描 `client/app/` 下 29 个 `.vue` 文件 + 3 个指令 `.ts` 文件，0 error

## 使用方式

```vue
<!-- 防抖点击，500ms -->
<button v-debounce:click.500="handleSend">发送</button>

<!-- 安全 HTML 渲染（markdown + DOMPurify） -->
<div v-safe-html="message.content"></div>
```

无需任何 `<script setup>` 中的 import 声明。

## 涉及文件清单

| 操作                | 文件                                                |
| ------------------- | --------------------------------------------------- |
| 新建                | `client/app/plugins/directives.ts`                  |
| 编辑（移除 import） | `client/app/pages/home/components/StatsDialog.vue`  |
| 编辑（移除 import） | `client/app/pages/home/components/SkillsDialog.vue` |
| 编辑（移除 import） | `client/app/pages/home/components/LogsDialog.vue`   |
| 编辑（移除 import） | `client/app/pages/home/components/CronDialog.vue`   |
| 编辑（移除 import） | `client/app/components/chat/inputBox.vue`           |
| 编辑（移除 import） | `client/app/pages/home/components/ChatBox.vue`      |
| 删除                | `src/directives/debounce.ts`                        |
| 保留（唯一实现）    | `client/app/directives/debounce.ts`                 |
| 保留（唯一实现）    | `client/app/directives/safeHtml.ts`                 |

---

# 四、ESLint + Prettier 配置与 Pre-commit fix-then-check 流程

## 思路

前端代码统一放在 `client/` 下（Nuxt4 应用，源码在 `client/app/`）。ESLint 使用 flat config 格式，Prettier 通过 `eslint-plugin-prettier` 集成进 ESLint（`prettier/prettier: 'error'`），无需单独的 `.prettierrc`。pre-commit 提交前端文件时采用 **fix-then-check** 流程：先自动修正，再检查是否合规。

---

## ESLint 配置

文件：`client/eslint.config.mjs`（flat config 格式）

### 依赖（devDependencies）

| 包                              | 作用                                          |
| ------------------------------- | --------------------------------------------- |
| `eslint` ^10.8.1                | lint 引擎                                     |
| `@eslint/js` ^10.0.1            | JS 推荐规则集                                 |
| `typescript-eslint` ^8.67.0     | TS 推荐规则集                                 |
| `eslint-plugin-vue` ^10.10      | Vue flat/essential 规则集                     |
| `eslint-plugin-prettier` ^5.5.6 | Prettier 集成（`prettier/prettier: 'error'`） |
| `@eslint/json` ^2.0.1           | JSON lint                                     |
| `@eslint/markdown` ^8.0.3       | Markdown lint                                 |
| `globals` ^17.11.0              | 浏览器全局变量声明                            |

### 配置结构

```js
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";
import json from "@eslint/json";
import markdown from "@eslint/markdown";
import { defineConfig } from "eslint/config";
import eslintPluginPrettier from "eslint-plugin-prettier";

export default defineConfig([
  // 1. JS 基础规则（js/mjs/cjs/ts/mts/cts/vue）
  {
    files: ["**/*.{js,mjs,cjs,ts,mts,cts,vue}"],
    plugins: { js },
    extends: ["js/recommended"],
    languageOptions: { globals: globals.browser },
  },
  // 2. TypeScript 推荐规则
  tseslint.configs.recommended,
  // 3. Vue 规则（仅 .vue 文件，避免在非 SFC 上崩溃）
  ...pluginVue.configs["flat/essential"].map((c) => ({
    ...c,
    files: ["**/*.vue"],
  })),
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  // 4. JSON / JSONC / JSON5 lint
  {
    files: ["**/*.json"],
    plugins: { json },
    language: "json/json",
    extends: ["json/recommended"],
  },
  {
    files: ["**/*.jsonc"],
    plugins: { json },
    language: "json/jsonc",
    extends: ["json/recommended"],
  },
  {
    files: ["**/*.json5"],
    plugins: { json },
    language: "json/json5",
    extends: ["json/recommended"],
  },
  // 5. Markdown lint
  {
    files: ["**/*.md"],
    plugins: { markdown },
    language: "markdown/commonmark",
    extends: ["markdown/recommended"],
  },
  // 6. 全局忽略
  {
    ignores: [
      "node_modules",
      "dist",
      ".nuxt",
      ".output",
      ".data",
      "test-results",
      "playwright-report",
      "src-tauri/target",
      "src-tauri/gen",
      "coverage",
      ".git",
      ".husky",
      ".vscode",
      ".idea",
      ".cache",
      "*.min.*",
      "*.config.*",
      "*.lock",
      "*.svg",
      "*.webp",
      "*.gif",
      "*.png",
      "*.jpg",
      "*.jpeg",
      "*.ico",
      "*.toml",
      "*.txt",
    ],
  },
  // 7. Vue 专属规则
  {
    files: ["**/*.vue"],
    rules: {
      "vue/no-v-html": "error", // 禁止 v-html（XSS 防护，唯一例外是 v-safe-html 指令）
      "vue/multi-word-component-names": "off",
      "vue/no-mutating-props": "off",
    },
  },
  // 8. 测试文件放宽
  {
    files: ["**/__tests__/**", "**/clientLog.ts"],
    rules: { "no-console": "off", "@typescript-eslint/no-explicit-any": "off" },
  },
  // 9. 全局规则
  {
    rules: {
      "no-console": "warn",
      "no-undef": "off", // 交给 Nuxt 框架检查
      "@typescript-eslint/no-unsafe-function-type": "off",
      "@typescript-eslint/no-explicit-any": "warn",
      semi: ["error"],
    },
  },
  // 10. Prettier 集成
  {
    plugins: { prettier: eslintPluginPrettier },
    rules: { "prettier/prettier": "error" },
  },
  // 11. Markdown 关闭 prettier/prettier（eslint-plugin-prettier 无法解析 markdown AST）
  { files: ["**/*.md"], rules: { "prettier/prettier": "off" } },
  // 12. tsconfig.json 用 JSONC 解析（Nuxt 生成，含注释）
  { files: ["tsconfig.json"], language: "json/jsonc" },
]);
```

---

## Prettier 配置

本项目**没有** `.prettierrc` / `prettier.config.mjs` 文件，使用 Prettier 默认配置，通过 `eslint-plugin-prettier` 以 `prettier/prettier: 'error'` 规则集成进 ESLint。`eslint --fix` 会同时修正 lint 问题 + 格式问题。

### `.prettierignore`

文件：`client/.prettierignore`

```
node_modules
dist
.nuxt
.output
.data
test-results
playwright-report
src-tauri/target
src-tauri/gen
coverage
.git
pnpm-lock.yaml
*.min.*
*.svg
*.webp
*.gif
*.png
*.jpg
*.jpeg
*.ico
```

---

## Pre-commit：fix-then-check 流程

### 设计目标

提交前端文件时，pre-commit 先自动修正（prettier --write + eslint --fix），再检查是否合规（eslint check + vue-tsc）。如果自动修正改动了文件，hook 失败提示重新暂存；第二次运行时文件已干净，直接进入检查阶段。

### 涉及文件

| 文件                              | 作用                                         |
| --------------------------------- | -------------------------------------------- |
| `scripts/hooks/frontend-fix.sh`   | **Phase 1**：prettier --write + eslint --fix |
| `scripts/hooks/frontend-style.sh` | **Phase 2-3**：eslint check + vue-tsc        |
| `.pre-commit-config.yaml`         | hook 定义与编排                              |

### Phase 1：自动修正脚本 `frontend-fix.sh`

```bash
#!/usr/bin/env bash
# Frontend auto-fix gate for pre-commit: prettier --write + eslint --fix.
#
# If either tool modifies a file, pre-commit detects the modification and fails
# the hook. Re-stage with `git add` and retry — on the second run the files
# are already fixed, so this hook passes and the check hooks (eslint, vue-tsc)
# run next.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

# Collect staged frontend files (strip client/ prefix)
files=()
for p in "$@"; do
  if [[ $p == client/* ]]; then
    files+=("${p#client/}")
  fi
done

if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

# Step 1: prettier --write (covers ts/vue/css/scss/json that eslint cannot)
pnpm exec prettier --write "${files[@]}"

# Step 2: eslint --fix (lint + fix; don't fail on unfixable issues —
# the eslint check hook catches those)
pnpm exec eslint --fix "${files[@]}" || true
```

### Phase 2-3：检查脚本 `frontend-style.sh`（已有，无需修改）

```bash
#!/usr/bin/env bash
# Frontend style gate for pre-commit: runs the client/ pnpm toolchain.
set -euo pipefail

tool="$1"
shift

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

args=()
for p in "$@"; do
  if [[ $p == client/* ]]; then
    args+=("${p#client/}")
  else
    args+=("$p")
  fi
done

exec pnpm exec "$tool" "${args[@]}"
```

### `.pre-commit-config.yaml` 前端 hook 定义

```yaml
# ---- frontend: fix-then-check (client/ pnpm toolchain) ------------------
- repo: local
  hooks:
    # Phase 1: auto-fix — prettier --write + eslint --fix
    - id: frontend-fix
      name: prettier + eslint (auto-fix)
      entry: bash scripts/hooks/frontend-fix.sh
      language: system
      require_serial: true
      files: ^client/
      types_or: [ts, tsx, vue, javascript, jsx, css, scss, json]
    # Phase 2: check-only — eslint catches unfixable lint issues
    - id: eslint
      name: eslint (check)
      entry: bash scripts/hooks/frontend-style.sh eslint
      language: system
      require_serial: true
      files: ^client/
      types_or: [ts, tsx, vue, javascript, jsx]
    # Phase 3: type check — vue-tsc on the whole project
    - id: vue-tsc
      name: vue-tsc (client types, whole project)
      entry: bash scripts/hooks/frontend-style.sh vue-tsc --noEmit -p .nuxt/tsconfig.app.json
      language: system
      pass_filenames: false
      require_serial: true
      files: ^client/.*\.(ts|tsx|vue|js|mjs|json)$
```

### 提交流程

```
git commit (前端文件)
  │
  ▼
Phase 1: frontend-fix.sh
  prettier --write → eslint --fix
  │
  ├─ 文件被修改？→ pre-commit 失败
  │    → git add 重新暂存 → 重新 commit → 回到 Phase 1
  │
  ▼ (文件无修改，Phase 1 通过)
Phase 2: eslint (check-only)
  │
  ├─ 有 unfixable lint 问题？→ 失败，手动修复
  │
  ▼ (lint 通过)
Phase 3: vue-tsc --noEmit
  │
  ├─ 有类型错误？→ 失败，手动修复
  │
  ▼ (全部通过)
commit 成功
```

---

# 五、Commitlint：提交信息强制 Angular 风格

## 背景

项目提交信息缺乏统一格式，`git log` 阅读性差。需要通过 pre-commit 的 `commit-msg` 阶段拦截不合规的提交信息，强制使用 Angular Commit Convention。

## 思路

使用 `@commitlint/cli` + `@commitlint/config-angular` 作为校验引擎。commitlint 通过 pre-commit 的 `commit-msg` stage 运行：pre-commit 将 `.git/COMMIT_EDITMSG` 文件路径传给 hook 脚本，脚本调用 `commitlint --edit <file>` 校验内容。

关键点：

- pre-commit 默认只安装 `pre-commit` hook，需要通过 `default_install_hook_types` 同时安装 `commit-msg` hook
- commitlint 配置文件和依赖放在 `client/` 下（与 ESLint/Prettier 统一使用 pnpm 工具链）

## Angular 提交规范

```
type(scope): subject

body

footer
```

### 允许的 type

| type       | 说明                         |
| ---------- | ---------------------------- |
| `feat`     | 新功能                       |
| `fix`      | Bug 修复                     |
| `docs`     | 文档变更                     |
| `style`    | 代码格式（不影响功能）       |
| `refactor` | 重构（非新功能、非修复 Bug） |
| `perf`     | 性能优化                     |
| `test`     | 测试相关                     |
| `build`    | 构建系统或外部依赖变更       |
| `ci`       | CI 配置变更                  |
| `chore`    | 杂项（不修改源码或测试）     |
| `revert`   | 回滚提交                     |

### 示例

```
feat(home): 添加技能管理弹窗
fix(chat): 修复消息流式输出重复内容
docs(api): 更新工具调用文档
refactor(agent): 抽取 subagent 公共生命周期逻辑
chore: 升级依赖版本
```

## 实际操作

### 1. 安装依赖

`client/package.json` devDependencies 新增：

```json
"@commitlint/cli": "^19.8.1",
"@commitlint/config-angular": "^19.8.1",
```

### 2. 创建配置文件 `client/commitlint.config.mjs`

```js
export default {
  extends: ["@commitlint/config-angular"],
  rules: {
    // Override: allow header up to 100 chars (Angular default is 100)
    "header-max-length": [2, "always", 100],
    // Allow Chinese in subject (Angular config defaults to English-only)
    "subject-case": [0],
  },
};
```

> 关闭 `subject-case` 规则是因为项目使用中文 subject（如"添加技能管理弹窗"），Angular 默认要求英文大小写格式。

### 3. 创建 hook 脚本 `scripts/hooks/commitlint.sh`

```bash
#!/usr/bin/env bash
# Commit message lint gate for pre-commit (commit-msg stage).
#
# Runs @commitlint/cli with @commitlint/config-angular to enforce Angular
# commit convention on the commit message.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../client"

# Pre-commit passes the path to .git/COMMIT_EDITMSG as $1
exec pnpm exec commitlint --edit "$1"
```

### 4. 更新 `.pre-commit-config.yaml`

```yaml
# 同时安装 pre-commit 和 commit-msg 两种 hook
default_install_hook_types: [pre-commit, commit-msg]

repos:
  # ... (ruff / basedpyright / frontend hooks 不变) ...

  # ---- commit message: commitlint (Angular convention) -------------------
  - repo: local
    hooks:
      - id: commitlint
        name: commitlint (angular)
        entry: bash scripts/hooks/commitlint.sh
        language: system
        stages: [commit-msg]
```

### 5. 安装 hook

```bash
uv run pre-commit install    # 自动写入 .git/hooks/pre-commit + .git/hooks/commit-msg
```

`default_install_hook_types` 配置使得一次 `install` 同时安装两种 hook 类型。

## 校验效果

```
# ❌ 不合规 — 缺少 type
git commit -m "更新了首页"
→ commitlint 拦截：type 必须是 feat/fix/docs/... 之一

# ❌ 不合规 — type 不在允许列表
git commit -m "update: 更新首页"
→ commitlint 拦截：type "update" 不在允许列表

# ❌ 不合规 — subject 为空
git commit -m "feat: "
→ commitlint 拦截：subject 不能为空

# ✅ 合规
git commit -m "feat(home): 添加技能管理弹窗"
→ commitlint 通过
```

## 涉及文件清单

| 操作 | 文件                                                                      |
| ---- | ------------------------------------------------------------------------- |
| 新建 | `client/commitlint.config.mjs`                                            |
| 新建 | `scripts/hooks/commitlint.sh`                                             |
| 编辑 | `client/package.json`（devDeps + script）                                 |
| 编辑 | `.pre-commit-config.yaml`（default_install_hook_types + commitlint hook） |

---

