# client_future

## 概述

`client_future` 是 EMA AI Agent 的下一代前端，定位为**流式 SPA 桌面客户端**，将逐步替换现有的 Python/Streamlit 前端（`client/` 目录）。

当前 `client/` 是 Python 3.10 + Streamlit 构建的会话式 Web UI，功能完整但受限于 Streamlit 的回滚式渲染模型，在交互流畅度、状态管理和跨平台桌面体验上存在天花板。`client_future` 以 **Tauri 2 + Nuxt 4** 重新构建，目标是：

- **更流畅的交互** — 不再每次操作全页回滚，Vue 3 响应式驱动局部更新
- **离线优先** — Dexie.js（IndexedDB）存储会话历史，减少网络依赖
- **原生桌面能力** — Tauri 2 提供系统托盘、文件系统、快捷键等 Streamlit 无法实现的功能
- **组件化架构** — Vue 3 组合式 API + Pinia 状态管理，便于团队协作扩展

> **开发状态**：项目尚在早期，部分组件为占位或演示形态。

---

## 目录结构

```
client_future/
├── .gitignore                    # Git 忽略规则
├── .vscode/                      # VS Code 工作区设置
│   └── settings.json
├── app/                          # Nuxt 4 SPA 源码
│   ├── app.vue                   # 根组件入口
│   ├── common.scss               # 全局 SCSS mixins 库（布局、形状、滚动条等）
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css          # 全局 CSS 重置 + CSS 变量
│   │   │   ├── main.scss         # (预留)
│   │   │   └── tailwind.scss     # Tailwind 指令注入（@tailwind base/components/utilities）
│   │   ├── images/               # (预留) 静态图片资源
│   │   └── ts/
│   │       └── tailwind.config.ts # Tailwind 自定义 token（宽度、高度、z-index 工具类）
│   ├── components/
│   │   ├── dom/                  # 基于 DOM 的 UI 组件
│   │   │   └── drawer.vue        # 抽屉面板组件
│   │   ├── icon/                 # (预留) 图标组件
│   │   └── svg/                  # SVG 图形组件
│   │       ├── staff.vue         # 单行五线谱（5 条线，由 lineGap/lineBold 参数控制）
│   │       └── staffPaper.vue    # 多行五线谱纸（基于 StaffConfig 计算行数/间距）
│   ├── composables/              # Vue 3 组合式逻辑
│   │   ├── mitt.ts               # mitt 事件总线实例
│   │   └── staffConfig.ts        # 五线谱配置单例（StaffConfig）— 响应式 ref 管理 paddingY/staffNum/heightPercent/gapPerStaff
│   ├── declare/                  # (预留) 类型声明
│   │   └── declarations.d.ts
│   ├── layouts/
│   │   └── default.vue           # 默认布局 — Nuxt 4 layout 入口
│   └── pages/
│       └── index.vue             # 主页 — 组合 LazySvgStaffPaper + DomDrawer
├── eslint.config.mjs             # ESLint 扁平化配置
├── node_modules/                 # pnpm 依赖（已 gitignore）
├── nuxt.config.ts                # Nuxt 4 配置（SSR=off, Vite, Tailwind CSS module）
├── package.json                  # 依赖清单（pnpm workspace 根）
├── pnpm-lock.yaml                # pnpm 锁定文件
├── pnpm-workspace.yaml           # pnpm 工作区定义
├── prettier.config.mjs           # Prettier 代码格式化配置
├── public/                       # Nuxt 公共静态资源
├── README.md                     # 本文（英文版）
├── README.zh.md                  # 本文（中文版）
├── src-tauri/                    # Tauri 2 原生壳
│   ├── capabilities/
│   │   └── default.json          # 权限配置（当前仅 core:default）
│   ├── icons/                    # 应用图标
│   ├── src/
│   │   ├── lib.rs                # Tauri 应用入口 — Builder setup + tauri_plugin_log（debug 模式）
│   │   └── main.rs               # Windows 子系统入口 + 调用 lib::run()
│   ├── Cargo.toml                # Rust 依赖（tauri 2, serde, serde_json, log, tauri-plugin-log）
│   ├── tauri.conf.json           # Tauri 2 配置 — 应用名 "anon"、构建命令、开发 URL localhost:3000、窗口配置、CSP
│   └── build.rs                  # Tauri 构建脚本
└── tsconfig.json                 # TypeScript 配置
```

---

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| **跨平台壳** | [Tauri 2](https://v2.tauri.app/) | 将 Web 前端打包为原生桌面应用，提供系统 API |
| **前端框架** | [Nuxt 4](https://nuxt.com/) + [Vue 3](https://vuejs.org/) | SPA 模式（`ssr: false`），组合式 API + `<script setup lang="ts">` |
| **状态管理** | [Pinia](https://pinia.vuejs.org/) + [pinia-plugin-persistedstate](https://prazdevs.github.io/pinia-plugin-persistedstate/) | 全局状态 + 持久化 |
| **样式** | [Tailwind CSS](https://tailwindcss.com/)（通过 `@nuxtjs/tailwindcss`）+ SCSS | 原子化 CSS + 自定义 Mixin 库 |
| **数据可视化** | [D3.js v7](https://d3js.org/) | SVG 乐谱渲染、知识图谱等 |
| **离线存储** | [Dexie.js](https://dexie.org/) | IndexedDB 封装，缓存会话历史 |
| **事件总线** | [mitt](https://github.com/developit/mitt) | 组件间轻量通信 |
| **工具库** | [lodash-es](https://lodash.com/) | 深拷贝、去重等常用工具函数 |
| **构建工具** | [Vite](https://vitejs.dev/) | 开发服务器 + 生产构建 |
| **后端语言** | [Rust](https://www.rust-lang.org/) 2021 edition | Tauri 原生逻辑 |
| **日志** | [log](https://docs.rs/log/) + [tauri-plugin-log](https://github.com/tauri-apps/tauri-plugin-log) | Tauri 后端日志（debug 模式下启用） |

### 关键配置

- **Nuxt**: `ssr: false`，纯 SPA；`pages/` 目录结构；Vite 配置 `process.env.TAURI_*` 环境变量前缀白名单
- **Tauri**: 应用标识 `com.anon.dev`，开发 URL `http://localhost:3000`，窗口标题 `anon`，CSP 设为空（`null`）以允许内联样式
- **Tailwind**: 通过 `@nuxtjs/tailwindcss` 模块引入，配置文件位于 `app/assets/ts/tailwind.config.ts`，提供 `w-*`/`h-*`/`z-*` 等自定义工具类

---

## 架构说明

### 与 client/ 的关系

```
当前状态：      client/ (Python+Streamlit) — 生产运行中
                    │
                    ├── 功能完整，但受限于 Streamlit 渲染模型
                    ├── Web Only，无桌面能力
                    └── 状态管理薄弱
                         │
               client_future/ (Tauri+Nuxt+Vue) — 开发中
                         │
                         ├── 流式 SPA，局部刷新
                         ├── 原生桌面应用（Tauri 2）
                         ├── 离线缓存（Dexie/IndexedDB）
                         └── 组件化 Pinia 状态管理
```

`client_future` **不会**一蹴而就替换 `client/`，而是逐步按模块迁移，最终 `client/` 将被废弃。

### 组件分层

```
app.vue (根)
  └─ NuxtLayout (default.vue)
       └─ NuxtPage (index.vue)
            ├─ LazySvgStaffPaper  (SVG 五线谱纸)
            │    └─ SvgStaff × N  (单行五线谱)
            └─ DomDrawer          (抽屉面板)
```

- **SVG 层** (`components/svg/`) — 五线谱/乐谱/知识图谱的向量渲染。`staff.vue` 渲染单行五线谱（5 条线），`staffPaper.vue` 根据 `StaffConfig` 单例计算页内行数和间距，组合多行。
- **DOM 层** (`components/dom/`) — 传统 HTML 组件（抽屉、按钮、面板等）。
- **Composables** — `staffConfig.ts` 采用**单例模式**保持全局一致的五线谱参数（paddingY, staffNum, heightPercent, gapPerStaff），通过 `ref` 实现响应式联动。`mitt.ts` 导出全局 `emitter` 实例。

### 数据流

```
用户交互 (Vue 组件)
    → Pinia Store (状态变更)
    → 响应式更新 UI
    → (可选) Tauri API (桌面能力)
    → (可选) Dexie.js (IndexedDB 持久化)
    → (未来) 后端 API 通信
```

---

## 核心模块说明

### StaffConfig 单例 (`composables/staffConfig.ts`)

五线谱渲染的参数管理中心。采用**单例模式**确保全局使用同一配置实例，所有参数通过 `ref()` 响应式暴露，任何组件修改参数后所有依赖方自动更新。

核心参数：
- `paddingY` — 页面上边距
- `staffNum` — 每页五线谱行数
- `heightPercent` — 单行高度百分比
- `gapPerStaff` — 行间间距
- `baisPerStaff` — 每行偏移量（用于滚动/分页）

### SvgStaff / SvgStaffPaper (`components/svg/`)

- **SvgStaff** — 渲染 5 条线的单行五线谱。`lineGap = heightPercent / 5`，`lineBold = heightPercent / 50`，以 `translate(x, y)` 定位。
- **SvgStaffPaper** — 组合 `SvgStaff` 多行显示。通过 `staffNumOfcurrentPage` 控制行数，`baisPerStaff` 控制行间距。`viewBox="0 0 100 100"` 实现响应式缩放。

### DomDrawer (`components/dom/drawer.vue`)

抽屉面板组件，用于侧边栏/浮层面板等 UI 模式。

### SCSS 工具库 (`common.scss`)

300+ 行的 SCSS mixin 库，提供布局、形状、滚动条、文本溢出等工具类：
- 尺寸限定：`minWidth` / `maxWidth` / `fixedWidth` / `fullWidth` 等
- 形状：`fixedRoundedRectangle` / `fixedCircle` / `fixedCapsule` 等
- 布局：`flexCenter` / `scrollBar` / `wordEllipsis` 等
- 图片：`imgFullInParent` / `fullImg` 等

### Tauri 后端 (`src-tauri/`)

- **lib.rs**: `tauri::Builder` 启动，debug 模式下加载 `tauri_plugin_log`
- **main.rs**: Windows 子系统入口，`#![windows_subsystem = "windows"]` 隐藏发布版控制台窗口
- **tauri.conf.json**: 应用标识 `com.anon.dev`，构建命令指向 `npm run build`，开发 URL `http://localhost:3000`
- **Cargo.toml**: Rust 依赖 — tauri 2.x (features: devtools), serde + serde_json (序列化), log + tauri-plugin-log (日志)

---

## 与现有 client/ 对比

| 维度 | client/ | client_future |
|------|---------|---------------|
| 语言 | Python 3.10 | TypeScript / Rust |
| 框架 | Streamlit | Nuxt 4 + Vue 3 |
| 渲染模式 | 全页回滚 (Script Re-run) | 响应式局部更新 |
| 桌面支持 | 无（仅 Web） | Tauri 2 原生桌面 |
| 存储 | Python 内存 (ChatStorage) | Dexie.js IndexedDB |
| 状态管理 | Streamlit Session State | Pinia + 持久化插件 |
| 可视化 | Streamlit 原生图表 | D3.js SVG |
| 事件 | N/A | mitt 事件总线 |
| 后端语言 | Python (FastAPI/WebSocket) | — (调用相同后端 API) |

---

## 开发指南

### 环境要求

- [Node.js](https://nodejs.org/) >= 18
- [pnpm](https://pnpm.io/)（推荐）或 npm
- [Rust](https://www.rust-lang.org/) (latest stable)
- [Tauri CLI v2](https://v2.tauri.app/start/cli/)

### 常用命令

```bash
# 安装依赖
pnpm install

# 开发模式（Web 浏览器）
pnpm dev

# Tauri 桌面开发模式
pnpm tauri dev

# 生产构建
pnpm build

# Tauri 桌面构建
pnpm tauri build
```

### 添加新页面/组件

1. 在 `app/pages/` 下创建 `.vue` 文件 — Nuxt 4 自动注册路由
2. 在 `app/components/` 下创建组件 — 自动全局可用
3. 在 `app/composables/` 下创建组合式逻辑
4. 在 `app/assets/ts/tailwind.config.ts` 中添加自定义 token

---

## 许可证

随 EMA AI Agent 主项目许可证。
