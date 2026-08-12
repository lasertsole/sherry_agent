# Git Hooks 可复用实现 — 完整指南

> 本文档从 RetailMiniAPP 项目中提炼，提供了基于 husky 9 + lint-staged 的 Git Hooks 完整实现方案，包括 pre-commit 代码检查、commit-message 规范校验、以及 patch-package 自动补丁。

---

## 目录

1. [整体架构](#1-整体架构)
2. [安装依赖](#2-安装依赖)
3. [husky 9 配置](#3-husky-9-配置)
4. [lint-staged 配置](#4-lint-staged-配置)
5. [commitlint 配置（可选增强）](#5-commitlint-配置可选增强)
6. [patch-package 配置](#6-patch-package-配置)
7. [完整 package.json 集成](#7-完整-packagejson-集成)
8. [异构项目适配指南](#8-异构项目适配指南)
9. [常见问题排查](#9-常见问题排查)

---

## 1. 整体架构

```
开发者执行 git commit
  │
  ▼
┌─────────────────────────────────────────┐
│  husky pre-commit 钩子                   │
│  └─ npx lint-staged                      │
│       └─ 对暂存文件执行：                 │
│            ├─ eslint --fix  (.vue/.js/.ts)│
│            └─ prettier --write (.vue/.js/.ts)│
└──────────────────┬──────────────────────┘
                   │ 全部通过
                   ▼
┌─────────────────────────────────────────┐
│  husky commit-msg 钩子（可选增强）        │
│  └─ commitlint --edit                   │
│       └─ 校验 commit message 格式        │
│            例: feat: 添加用户登录功能      │
└──────────────────┬──────────────────────┘
                   │ 格式正确
                   ▼
              commit 成功

独立流程（npm install 时触发）：
┌─────────────────────────────────────────┐
│  postinstall 脚本                        │
│  └─ patch-package                        │
│       └─ 自动应用 patches/ 目录下的补丁    │
└─────────────────────────────────────────┘
```

---

## 2. 安装依赖

```bash
# husky 9 — Git 钩子管理
npm install -D husky

# lint-staged — 仅对暂存文件执行检查
npm install -D lint-staged

# patch-package — 第三方依赖补丁
npm install -D patch-package

# commitlint（可选）— commit message 规范校验
npm install -D @commitlint/cli @commitlint/config-conventional
```

---

## 3. husky 9 配置

### 3.1 初始化 husky

```bash
# 执行初始化命令（会创建 .husky/ 目录并在 package.json 中添加 prepare 脚本）
npx husky init
```

该命令会：

1. 创建 `.husky/` 目录
2. 在 `package.json` 中添加 `"prepare": "husky"` 脚本
3. 创建 `.husky/pre-commit` 示例文件

### 3.2 pre-commit 钩子

创建/编辑 `.husky/pre-commit` 文件：

```sh
# .husky/pre-commit

npx --no-install -- lint-staged
```

> **husky 9 语法说明**：husky 9 不再需要 `#!/usr/bin/env sh` 和 `. "$(dirname -- "$0")/_/husky.sh"` 头部，直接写命令即可。

### 3.3 commit-msg 钩子（可选增强）

创建 `.husky/commit-msg` 文件：

```sh
# .husky/commit-msg

npx --no-install -- commitlint --edit "$1"
```

### 3.4 钩子文件权限

确保钩子文件有可执行权限（Linux/macOS）：

```bash
chmod +x .husky/pre-commit
chmod +x .husky/commit-msg
```

> **Windows 用户**：无需手动设置权限，Git for Windows 会自动处理。

---

## 4. lint-staged 配置

### 4.1 方式一：在 package.json 中配置（推荐，简单项目）

```json
// package.json
{
  "scripts": {
    "lint-staged": "lint-staged"
  },
  "lint-staged": {
    "**/*.{vue,js,ts}": ["eslint --fix", "prettier --write"]
  }
}
```

### 4.2 方式二：独立配置文件（推荐，复杂项目）

创建 `.lintstagedrc.mjs`：

```javascript
// .lintstagedrc.mjs
export default {
  // Vue / JS / TS 文件：ESLint 修复 + Prettier 格式化
  "**/*.{vue,js,ts}": ["eslint --fix", "prettier --write"],

  // CSS / SCSS / LESS 文件：Prettier 格式化
  "**/*.{css,scss,less}": ["prettier --write"],

  // JSON 文件：Prettier 格式化
  "**/*.{json,jsonc,json5}": ["prettier --write"],

  // HTML 文件：Prettier 格式化
  "**/*.html": ["prettier --write"],

  // Markdown 文件：Prettier 格式化
  "**/*.md": ["prettier --write"],
};
```

### 4.3 方式三：使用函数配置（高级，需要条件判断）

创建 `lint-staged.config.mjs`：

```javascript
// lint-staged.config.mjs
export default {
  "**/*.{vue,js,ts}": async (files) => {
    // 可以根据文件数量决定是否分批处理
    if (files.length > 50) {
      // 文件太多时只检查不修复，避免超时
      return `eslint --no-fix ${files.join(" ")}`;
    }
    return ["eslint --fix", "prettier --write"];
  },
};
```

### 4.4 RetailMiniAPP 实际配置

```json
// package.json (RetailMiniAPP 实际使用的配置)
{
  "scripts": {
    "lint-staged": "lint-staged",
    "prepare": "husky"
  },
  "lint-staged": {
    "**/*.{vue,js,ts}": ["eslint --fix", "prettier --write"]
  },
  "devDependencies": {
    "husky": "^9.1.7",
    "lint-staged": "^15.5.1"
  }
}
```

---

## 5. commitlint 配置（可选增强）

### 5.1 为什么需要 commitlint

RetailMiniAPP 项目**没有**配置 commitlint，但推荐新项目添加，好处是：

- 强制 commit message 遵循约定式提交规范（Conventional Commits）
- 自动生成 changelog
- 便于后续自动化版本管理

### 5.2 配置文件

创建 `commitlint.config.mjs`：

```javascript
// commitlint.config.mjs
export default {
  extends: ["@commitlint/config-conventional"],

  // 自定义规则
  rules: {
    // type 枚举（必须使用以下之一）
    "type-enum": [
      2,
      "always",
      [
        "feat", // 新功能
        "fix", // 修复 bug
        "docs", // 文档变更
        "style", // 代码格式（不影响功能）
        "refactor", // 重构（既不是 feat 也不是 fix）
        "perf", // 性能优化
        "test", // 添加/修改测试
        "build", // 构建系统或外部依赖变更
        "ci", // CI 配置变更
        "chore", // 其他杂项
        "revert", // 回退 commit
      ],
    ],

    // type 不能为空
    "type-empty": [2, "never"],

    // subject 不能为空
    "subject-empty": [2, "never"],

    // subject 不能超过 100 字符
    "subject-max-length": [2, "always", 100],

    // subject 不以句号结尾
    "subject-full-stop": [0],

    // subject 大小写（不强制）
    "subject-case": [0],
  },
};
```

### 5.3 commit message 格式

```text
<type>(<scope>): <subject>

<body>

<footer>
```

**示例**：

```text
feat(auth): 添加用户登录功能

支持手机号+验证码登录和账号密码登录两种方式

Closes #123
```

### 5.4 合规/不合规示例

```text
✅ feat: 添加用户登录功能
✅ fix(auth): 修复 token 过期未跳转登录页的问题
✅ docs: 更新 README 安装说明
✅ refactor(utils): 重构日期格式化工具函数
✅ perf: 优化列表渲染性能

❌ 添加登录功能                    （缺少 type 前缀）
❌ feat: 添加登录功能.             （subject 以句号结尾 — 如果启用了 subject-full-stop 规则）
❌ feat:                           （subject 为空）
❌ wip: 临时提交                   （type 不在枚举中）
❌ 添加用户登录功能以及注册功能并修复了若干bug   （subject 过长 — 超过 100 字符）
```

---

## 6. patch-package 配置

### 6.1 场景

第三方 npm 包有 bug 但无法等待官方修复，或者需要对第三方包做定制化修改。

### 6.2 配置

```json
// package.json
{
  "scripts": {
    "postinstall": "patch-package"
  },
  "devDependencies": {
    "patch-package": "8.0.0"
  }
}
```

### 6.3 使用流程

```bash
# 步骤 1：直接修改 node_modules 中的文件
# 例如修改 node_modules/some-package/dist/index.js

# 步骤 2：生成补丁文件
npx patch-package some-package
# 自动生成 patches/some-package+1.2.3.patch

# 步骤 3：提交补丁到 git
git add patches/
git commit -m "fix: patch some-package for bug fix"
```

### 6.4 补丁文件示例

```
patches/
├── @materials+approve-button-group-vue3+0.0.30.patch
└── @materials+form-render-vue3+0.0.10.patch
```

补丁文件是标准 git diff 格式：

```diff
diff --git a/node_modules/@materials/form-render-vue3/dist/index.js b/node_modules/@materials/form-render-vue3/dist/index.js
index 1234567..abcdefg 100644
--- a/node_modules/@materials/form-render-vue3/dist/index.js
+++ b/node_modules/@materials/form-render-vue3/dist/index.js
@@ -100,7 +100,7 @@
-    if (formItem.status === 'HIDDEN' || formItem.status === 'READONLY') {
+    if (formItem.finalStatus === 'HIDDEN' || formItem.finalStatus === 'READONLY') {
```

### 6.5 团队协作流程

```
开发者 A 修复了第三方包 bug
  │
  ├─ npx patch-package some-package  → 生成 patches/some-package+1.2.3.patch
  ├─ git add patches/ && git commit && git push
  │
开发者 B 拉取代码
  │
  ├─ npm install
  │    └─ postinstall 自动执行 patch-package
  │         └─ 自动应用 patches/ 目录下的所有补丁
  │
  └─ 依赖 bug 已自动修复 ✅
```

---

## 7. 完整 package.json 集成

以下是集成了 husky + lint-staged + commitlint + patch-package 的完整 package.json 片段：

```json
{
  "name": "my-project",
  "version": "1.0.0",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "lint": "eslint .",
    "format": "prettier --write .",
    "lint-staged": "lint-staged",
    "prepare": "husky",
    "postinstall": "patch-package",
    "test": "echo \"no test specified\" && exit 0"
  },
  "lint-staged": {
    "**/*.{vue,js,ts}": ["eslint --fix", "prettier --write"],
    "**/*.{css,scss,less,json,html,md}": ["prettier --write"]
  },
  "devDependencies": {
    "@commitlint/cli": "^19.3.0",
    "@commitlint/config-conventional": "^19.2.2",
    "eslint": "^9.25.1",
    "eslint-config-prettier": "^10.1.2",
    "eslint-plugin-prettier": "^5.2.6",
    "husky": "^9.1.7",
    "lint-staged": "^15.5.1",
    "patch-package": "8.0.0",
    "prettier": "^3.6.2",
    "typescript": "^5.8.3"
  }
}
```

---

## 8. 异构项目适配指南

### 8.1 React 项目

```json
// package.json
{
  "lint-staged": {
    "**/*.{jsx,tsx,js,ts}": ["eslint --fix", "prettier --write"],
    "**/*.{css,scss,json,md}": ["prettier --write"]
  }
}
```

pre-commit 钩子和 husky 配置完全相同，无需修改。

### 8.2 Angular 项目

```json
// package.json
{
  "lint-staged": {
    "**/*.ts": ["ng lint --fix", "prettier --write"],
    "**/*.{html,css,scss,json,md}": ["prettier --write"]
  }
}
```

### 8.3 Vite 项目

配置完全相同。Vite 项目通常使用 ESLint 9 flat config，与 lint-staged 完美兼容。

### 8.4 Node.js 后端项目

```json
// package.json
{
  "lint-staged": {
    "**/*.{js,ts}": ["eslint --fix", "prettier --write"]
  }
}
```

### 8.5 Monorepo 项目（pnpm workspace）

```yaml
# pnpm-workspace.yaml
packages:
  - "packages/*"
```

```json
// 根 package.json
{
  "scripts": {
    "prepare": "husky",
    "postinstall": "patch-package"
  },
  "lint-staged": {
    "packages/**/*.{ts,tsx,js,jsx}": ["eslint --fix", "prettier --write"]
  }
}
```

```sh
# .husky/pre-commit
npx --no-install -- lint-staged
```

---

## 9. 常见问题排查

### 9.1 husky 钩子不触发

```bash
# 检查 husky 是否已安装
npx husky --version

# 重新初始化
npx husky init

# 检查 .husky/ 目录结构
ls -la .husky/

# 确认 prepare 脚本存在
cat package.json | grep prepare
# 应输出: "prepare": "husky"

# 手动运行 prepare
npm run prepare
```

### 9.2 lint-staged 报错 "No staged files found"

```bash
# 确保文件已 git add
git add .

# 检查暂存区
git status

# 手动测试 lint-staged
npx lint-staged --debug
```

### 9.3 ESLint --fix 修改了文件但 commit 失败

这是因为 lint-staged 在修复文件后，会将修复后的内容重新 staged，但如果修复后的代码仍有 error，eslint 会返回非零退出码。

```bash
# 查看具体哪个文件报错
npx eslint src/problem-file.ts

# 方案1：手动修复剩余 error
# 方案2：在 ESLint 配置中降级某些规则为 warn
# 方案3：在 lint-staged 配置中只检查不修复
```

### 9.4 Windows 下的换行符问题

```bash
# .gitattributes 强制统一换行符
# 创建 .gitattributes 文件：
* text=auto eol=lf
*.bat text eol=crlf
*.cmd text eol=crlf

# Prettier 配置中设置 endOfLine: 'auto' 避免冲突
# prettier.config.mjs
export default {
  endOfLine: 'auto',
  // ...其他配置
};
```

### 9.5 patch-package 补丁应用失败

```bash
# 常见原因：npm 包版本更新导致补丁无法匹配
# 解决：重新生成补丁

# 1. 删除旧补丁
rm patches/some-package+1.2.3.patch

# 2. 更新依赖
npm install some-package@1.2.3

# 3. 重新修改 node_modules 中的文件
# 4. 重新生成补丁
npx patch-package some-package
```

### 9.6 commit-msg 钩子在 Windows 下路径问题

```sh
# .husky/commit-msg (Windows 兼容写法)
npx --no-install -- commitlint --edit "$1"
```

> `$1` 在 Windows Git Bash 环境下会被正确解析为 commit message 文件路径。

### 9.7 CI/CD 环境跳过 husky

```bash
# CI 环境通常不需要运行 husky 钩子
# 方式1：设置环境变量
export HUSKY=0

# 方式2：在 CI 配置中跳过 prepare 脚本
npm install --ignore-scripts

# 方式3：在 package.json 中条件执行
{
  "scripts": {
    "prepare": "husky || true"
  }
}
```

---

## 10. 快速接入检查清单

### 最小配置（仅 pre-commit）

- [ ] `npm install -D husky lint-staged`
- [ ] `npx husky init`
- [ ] 编辑 `.husky/pre-commit`，写入 `npx --no-install -- lint-staged`
- [ ] 在 `package.json` 中添加 `lint-staged` 配置
- [ ] 确认 `package.json` 有 `"prepare": "husky"` 脚本
- [ ] 测试：`git add . && git commit -m "test"` 验证钩子触发

### 完整配置（pre-commit + commit-msg + patch-package）

- [ ] `npm install -D husky lint-staged patch-package @commitlint/cli @commitlint/config-conventional`
- [ ] `npx husky init`
- [ ] 编辑 `.husky/pre-commit`，写入 `npx --no-install -- lint-staged`
- [ ] 创建 `.husky/commit-msg`，写入 `npx --no-install -- commitlint --edit "$1"`
- [ ] 创建 `commitlint.config.mjs`，配置提交规范
- [ ] 在 `package.json` 中添加 `lint-staged` 配置
- [ ] 在 `package.json` 中添加 `"postinstall": "patch-package"` 脚本
- [ ] 创建 `.gitattributes` 统一换行符
- [ ] 测试：提交一个不合规的 commit message 验证 commitlint 拦截
- [ ] 测试：提交一个合规的 commit message 验证全流程通过

### 团队推广

- [ ] 在 README 中记录 Git Hooks 配置说明
- [ ] 确保所有开发者执行 `npm install` 后 husky 自动初始化
- [ ] 在 CI/CD 中设置 `HUSKY=0` 跳过钩子（CI 有独立的检查流程）
- [ ] 对 patches/ 目录做 git review，确保补丁内容合理
