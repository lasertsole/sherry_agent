# client_future

## 概述

`client_future` 是 EMA AI Agent 的下一代前端，定位为**流式 SPA 桌面客户端**，将逐步替换现有的 Python/Streamlit 前端（`client/` 目录）。

当前 `client/` 是 Python 3.10 + Streamlit 构建的会话式 Web UI，功能完整但受限于 Streamlit 的回滚式渲染模型，在交互流畅度、状态管理和跨平台桌面体验上存在天花板。`client_future` 以 **Tauri 2 + Nuxt 4** 重新构建，目标是：

- **更流畅的交互** — 不再每次操作全页回滚，Vue 3 响应式驱动局部更新
- **离线优先** — Dexie.js（IndexedDB）存储会话历史，减少网络依赖
- **原生桌面能力** — Tauri 2 提供系统托盘、全局快捷键（Alt+Space）、文件系统等 Streamlit 无法实现的功能
- **组件化架构** — Vue 3 组合式 API + Pinia 状态管理，便于团队协作扩展

> **开发状态**：积极开发中；核心聊天 UI 和 Tauri IPC 桥接已可用。

---

## 架构说明

### 混合架构

```
+--------------------------------------------------------------------+
|                    Tauri 2 桌面应用 (client_future/)                  |
|                                                                    |
|  +-------------------+       invoke()       +--------------------+ |
|  |  Nuxt 4 前端      | ===================> |  Rust 后端层       | |
|  |  (app/)           |                      |  (src-tauri/src/)  | |
|  |                   | <=================== |                    | |
|  |  bridge.ts        |   Tauri Events       |  commands/         | |
|  |  (双模式)         |   (流式)             |  services/         | |
|  +-------------------+                      |  core/              | |
|                                             |  utils/             | |
|                                             +---------+----------+ |
|                                                       |            |
+-------------------------------------------------------|------------+
                                                         |
                                           reqwest HTTP (localhost:8080)
                                           SSE 流 / JSON REST
                                                         |
+-------------------------------------------------------|------------+
|                    Python 后端 (server/)                |            |
|                                                        v           |
|  Robyn HTTP + SSE + WebSocket                                      |
|  Agent 核心 (LangGraph) | RAG | 多通道 Bot         |
+--------------------------------------------------------------------+
```

**Rust 实现**：Rust 层实现了以下功能：
1. 接收前端的 Tauri IPC 调用
2. 以 HTTP 请求转发到 Python 后端（`http://127.0.0.1:8080`）
3. 将 Python 的 SSE 流转换为 Tauri Events，供前端实时更新
4. 管理 Python 后端进程生命周期（通过 `EMA_AUTO_START_BACKEND` 可选自动启动）
5. 提供系统托盘（显示/隐藏/退出）和全局快捷键（Alt+Space 切换窗口）

### 数据流

```
用户交互 (Vue 组件)
    -> bridge.ts (自动检测 Tauri / 浏览器模式)
        |
        |--> [Tauri 模式] invoke() -> Rust IPC 命令
        |        -> PythonBridge (reqwest HTTP) -> Python 后端
        |        -> SSE 流 -> Tauri Events -> 前端监听器
        |
        |--> [浏览器模式] fetchApi() -> Python 后端 (直接 SSE/REST)
        |
    -> Pinia Store (状态更新)
    -> 响应式 UI 更新
```

---

## 目录结构

```
client_future/
├── .env.example                   # 环境变量模板
├── .gitignore                     # Git 忽略规则
├── eslint.config.mjs              # ESLint 扁平化配置
├── nuxt.config.ts                 # Nuxt 4 配置（SSR=off, Vite, Tailwind, i18n, PrimeVue, color-mode）
├── package.json                   # 依赖清单（pnpm workspace 根）
├── pnpm-lock.yaml                 # pnpm 锁定文件
├── pnpm-workspace.yaml            # pnpm 工作区定义
├── prettier.config.mjs            # Prettier 代码格式化配置
├── tailwind.config.js             # Tailwind 配置
├── tsconfig.json                  # TypeScript 配置
├── app/                           # Nuxt 4 SPA 源码
│   ├── app.vue                    # 根组件入口
│   ├── common.scss                # 全局 SCSS mixins 库（布局、形状、滚动条等）
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css           # 全局 CSS 重置 + CSS 变量
│   │   │   ├── main.scss          # (预留)
│   │   │   └── tailwind.scss      # Tailwind 指令注入
│   │   ├── images/                # (预留) 静态图片资源
│   │   └── ts/
│   │       └── tailwind.config.ts # Tailwind 自定义 token
│   ├── common/
│   │   └── utils.ts               # 共享工具函数（formatCompactTimeString，基于 dayjs）
│   ├── components/
│   │   └── chat/
│   │       └── inputBox.vue       # 聊天输入框组件（支持 i18n）
│   ├── composables/               # Vue 3 组合式逻辑
│   │   ├── bridge.ts              # 统一的 Tauri/浏览器通信桥接
│   │   ├── messages.ts            # 消息 API：历史、清除会话、SSE 流式
│   │   ├── requestApi.ts          # HTTP 请求封装（useFetch + 重试逻辑）
│   │   ├── system.ts              # (预留) 系统组合式
│   │   ├── utils.ts               # 日期/时间工具（比较、格式化、UTC 转换）
│   │   ├── workspace.ts           # 系统提示词 & 角色 CRUD 操作
│   │   ├── ws.ts                  # WebSocket 单例，自动重连（5秒）
│   │   └── mitt.ts                # mitt 事件总线实例
│   ├── declare/
│   │   └── declarations.d.ts      # 类型声明
│   ├── i18n/
│   │   └── locales/
│   │       ├── en.json            # 英文翻译
│   │       └── zh.json            # 中文翻译
│   ├── layouts/
│   │   └── default.vue            # 默认布局 — Nuxt 4 layout 入口
│   ├── pages/
│   │   ├── index.vue              # 根页面（重定向到 /home）
│   │   └── home/
│   │       ├── index.vue          # 主聊天页 — 侧边栏 + 聊天区域
│   │       ├── config.ts          # 工具栏 & 头部工具配置
│   │       ├── type.ts            # Session/Message/ChatRole 类型定义
│   │       └── components/
│   │           ├── ChatBox.vue    # 消息列表，支持 markdown 渲染 & XSS 净化
│   │           ├── HistoryItem.vue# 侧边栏历史会话项
│   │           └── ModeSwitch.vue # 深色/浅色模式切换（PrimeVue ToggleSwitch）
│   └── types/
│       ├── message.ts             # 消息类型定义（BaseMessage, AiMessage, MultiModalMessage 等）
│       └── response.d.ts          # API 响应类型定义
├── src-tauri/                     # Tauri 2 原生壳
│   ├── capabilities/
│   │   └── default.json           # 权限配置（当前仅 core:default）
│   ├── icons/                     # 应用图标
│   ├── src/
│   │   ├── lib.rs                 # Tauri 应用入口 — Builder 设置、托盘菜单、全局快捷键、Python 进程管理
│   │   ├── main.rs                # Windows 子系统入口 + 调用 lib::run()
│   │   ├── commands/              # Tauri IPC 命令处理器
│   │   │   ├── mod.rs
│   │   │   ├── agent.rs           # agent_chat, agent_stop
│   │   │   ├── character.rs       # character_read/write/update
│   │   │   ├── events.rs          # 事件类型定义
│   │   │   ├── session.rs         # session_clear, session_history
│   │   │   ├── system.rs          # system_info, system_health
│   │   │   └── system_prompt.rs   # system_prompt_read/write/update
│   │   ├── services/
│   │   │   ├── python_bridge.rs   # HTTP 桥接（reqwest + SSE → Tauri Events）
│   │   │   └── python_process.rs  # Python 后端进程生命周期管理器
│   │   ├── core/                  # 核心领域模块（桩/占位）
│   │   │   ├── mod.rs
│   │   │   ├── agent/
│   │   │   ├── bus/
│   │   │   ├── channel/
│   │   │   ├── cron/
│   │   │   ├── heartbeat/
│   │   │   ├── memory/
│   │   │   └── subagent/
│   │   ├── utils/
│   │   │   ├── mod.rs
│   │   │   ├── config.rs          # AppConfig（从环境变量加载）
│   │   │   ├── error.rs           # 错误类型
│   │   │   └── logger.rs          # Tracing 日志设置
│   │   ├── config/
│   │   ├── database/
│   │   ├── models/
│   │   ├── prompts/
│   │   ├── rag/
│   │   ├── runtime/
│   │   ├── sessions/
│   │   ├── skills/
│   │   ├── tools/
│   │   └── types/
│   ├── Cargo.toml                 # Rust 依赖（tauri 2, serde, reqwest, tracing, ts-rs 等）
│   ├── tauri.conf.json            # Tauri 2 配置 — 应用名 "EMA AI Agent"，标识 "com.ema-ai.agent"
│   └── build.rs                   # Tauri 构建脚本
└── public/                        # Nuxt 公共静态资源
```

---

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| **跨平台壳** | [Tauri 2](https://v2.tauri.app/) | 将 Web 前端打包为原生桌面应用，提供系统 API |
| **前端框架** | [Nuxt 4](https://nuxt.com/) + [Vue 3](https://vuejs.org/) | SPA 模式（`ssr: false`），组合式 API + `<script setup lang="ts">` |
| **UI 组件** | [PrimeVue 4](https://primevue.org/) + [PrimeIcons](https://primevue.org/icons) | 预构建 UI 组件（Button、Checkbox、Menu、ToggleSwitch 等） |
| **状态管理** | [Pinia](https://pinia.vuejs.org/) + [pinia-plugin-persistedstate](https://prazdevs.github.io/pinia-plugin-persistedstate/) | 全局状态 + 持久化 |
| **样式** | [Tailwind CSS](https://tailwindcss.com/)（通过 `@nuxtjs/tailwindcss`）+ SCSS | 原子化 CSS + 自定义 Mixin 库 |
| **色彩模式** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | 深色/浅色主题切换 |
| **国际化** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) | 中文（默认）/ 英文 |
| **Markdown 渲染** | [markdown-it](https://github.com/markdown-it/markdown-it) | 聊天消息 Markdown → HTML |
| **XSS 防护** | [DOMPurify](https://github.com/cure53/DOMPurify) | 净化 HTML 输出 |
| **日期格式化** | [dayjs](https://day.js.org/) | 解析/格式化紧凑时间戳（YYYYMMDDHHmmss） |
| **离线存储** | [Dexie.js](https://dexie.org/) | IndexedDB 封装，缓存会话历史 |
| **事件总线** | [mitt](https://github.com/developit/mitt) | 组件间轻量通信 |
| **工具库** | [lodash-es](https://lodash.com/) | 深拷贝、去重等常用工具函数 |
| **构建工具** | [Vite](https://vitejs.dev/) | 开发服务器 + 生产构建 |
| **后端语言** | [Rust](https://www.rust-lang.org/) 2021 edition（MSRV 1.94） | Tauri 原生逻辑 |
| **日志** | [tracing](https://docs.rs/tracing/) + [tauri-plugin-tracing](https://github.com/tauri-apps/tauri-plugin-tracing) | 结构化日志（Tauri 后端） |
| **类型生成** | [ts-rs](https://github.com/Aleph-Alpha/ts-rs) | 从 Rust 结构体自动生成 TypeScript 类型 |

### 关键配置

- **Nuxt**: `ssr: false`，纯 SPA；`pages/` 目录结构；Vite 配置 `VITE_*` 和 `TAURI_*` 环境变量前缀白名单；路由 `/` 重定向到 `/home`
- **Tauri**: 应用标识 `com.ema-ai.agent`，产品名 "EMA AI Agent"，开发 URL `http://localhost:3000`，CSP 设为空（`null`）以允许内联样式，窗口 800×600 可调整大小
- **Tailwind**: 通过 `@nuxtjs/tailwindcss` 模块引入，配置文件位于 `app/assets/ts/tailwind.config.ts` 和根目录 `tailwind.config.js`，提供 `w-*`/`h-*`/`z-*` 等自定义工具类
- **i18n**: 默认语言 `zh`，策略 `prefix_except_default`
- **PrimeVue**: Noir 预设（slate 色板），深色模式通过 `.dark` CSS 类选择器触发

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
                         ├── 系统托盘 + 全局快捷键（Alt+Space）
                         ├── 离线缓存（Dexie/IndexedDB）
                         ├── WebSocket 实时推送
                         ├── Markdown 渲染 + XSS 净化
                         ├── 深色/浅色模式 + 国际化
                         └── 组件化 Pinia 状态管理
```

`client_future` **不会**一蹴而就替换 `client/`，而是逐步按模块迁移，最终 `client/` 将被废弃。

### 组件分层

```
app.vue (根)
  └─ NuxtLayout (default.vue)
       └─ NuxtPage
            ├─ / (重定向 → /home)
            └─ /home (home/index.vue)
                 ├─ HistoryItem (侧边栏会话列表)
                 ├─ ModeSwitch (深色/浅色切换)
                 └─ ChatBox (消息区域)
                      └─ Markdown 渲染 (markdown-it + DOMPurify)
```

### 通信桥接 (bridge.ts)

`bridge.ts` 组合式函数提供统一 API，同时支持 Tauri 桌面和浏览器模式：

| API | 说明 |
|-----|------|
| `sendChatMessage(request, onChunk)` | 流式 Agent 聊天（Tauri Events / SSE） |
| `stopChatMessage(sessionId)` | 停止正在进行的生成 |
| `clearSession(sessionId)` | 清除会话状态 |
| `getHistory(sessionId, lastTurnCount)` | 获取对话历史 |
| `readSystemPrompt()` | 读取所有系统提示词文件 |
| `writeSystemPrompt(fileToContent)` | 覆盖系统提示词文件 |
| `updateSystemPrompt(fileToContent)` | 合并更新系统提示词文件 |
| `readCharacter()` | 读取角色配置 |
| `writeCharacter(data)` | 覆盖角色配置 |
| `updateCharacter(data)` | 合并更新角色配置 |
| `checkHealth()` | 检查 Python 后端可达性 |

### WebSocket (ws.ts)

WebSocket 单例，用于实时服务端推送通知：

- 连接地址 `{wsBase}/sessions/ws?session_id=main`
- 断线自动重连（5 秒延迟）
- 通过 mitt 发射事件：`ws:connected`、`ws:disconnected`、`ws:notification`、`ws:message`
- 从 `VITE_API_BACK_URL` 环境变量解析 WS URL

---

## 核心模块说明

### SCSS 工具库 (`common.scss`)

300+ 行的 SCSS mixin 库，提供布局、形状、滚动条、文本溢出等工具类：
- 尺寸限定：`minWidth` / `maxWidth` / `fixedWidth` / `fullWidth` 等
- 形状：`fixedRoundedRectangle` / `fixedCircle` / `fixedCapsule` 等
- 布局：`flexCenter` / `scrollBar` / `wordEllipsis` 等
- 图片：`imgFullInParent` / `fullImg` 等

### Tauri 后端 (`src-tauri/`)

- **lib.rs**: `tauri::Builder` 启动，系统托盘（显示/隐藏/退出），全局快捷键（Alt+Space 切换窗口），Python 进程管理器（通过 `EMA_AUTO_START_BACKEND` 自动启动）
- **main.rs**: Windows 子系统入口，`#![windows_subsystem = "windows"]` 隐藏发布版控制台窗口
- **tauri.conf.json**: 应用标识 `com.ema-ai.agent`，产品名 "EMA AI Agent"，构建命令 `pnpm build`，开发 URL `http://localhost:3000`
- **Cargo.toml**: Rust 依赖 — tauri 2.x (tray-icon feature), serde + serde_json, reqwest (rustls-tls, stream), tracing + tauri-plugin-tracing, ts-rs, thiserror, anyhow, tokio, uuid，以及插件：shell, notification, global-shortcut, single-instance, window-state

### Rust 模块结构

```
src-tauri/src/
├── commands/          # IPC 命令处理器
│   ├── agent.rs       # agent_chat, agent_stop
│   ├── character.rs   # character_read/write/update
│   ├── session.rs     # session_clear, session_history
│   ├── system.rs      # system_info, system_health
│   └── system_prompt.rs
├── services/
│   ├── python_bridge.rs   # HTTP 桥接（reqwest + SSE → Tauri Events）
│   └── python_process.rs  # Python 后端进程管理器
├── core/              # 核心领域模块（桩/占位）
├── utils/
│   ├── config.rs      # 从环境变量加载 AppConfig
│   ├── error.rs       # 错误类型
│   └── logger.rs      # Tracing 日志设置
├── config/
├── database/
├── models/
├── prompts/
├── rag/
├── runtime/
├── sessions/
├── skills/
├── tools/
└── types/
```

---

## 与现有 client/ 对比

| 维度 | client/ | client_future |
|------|---------|---------------|
| 语言 | Python 3.10 | TypeScript / Rust |
| 框架 | Streamlit | Nuxt 4 + Vue 3 |
| UI 组件库 | Streamlit 原生 | PrimeVue 4 + Tailwind CSS |
| 渲染模式 | 全页回滚 (Script Re-run) | 响应式局部更新 |
| 桌面支持 | 无（仅 Web） | Tauri 2 原生桌面 |
| 存储 | Python 内存 (ChatStorage) | Dexie.js IndexedDB |
| 状态管理 | Streamlit Session State | Pinia + 持久化插件 |
| Markdown | N/A | markdown-it + DOMPurify |
| 可视化 | Streamlit 原生图表 | D3.js SVG |
| 事件 | N/A | mitt 事件总线 + WebSocket |
| 国际化 | N/A | @nuxtjs/i18n（中/英） |
| 深色模式 | N/A | @nuxtjs/color-mode |
| 后端语言 | Python (FastAPI/WebSocket) | — (调用相同后端 API) |

---

## 开发指南

### 环境要求

- [Node.js](https://nodejs.org/) >= 18
- [pnpm](https://pnpm.io/)（推荐）或 npm
- [Rust](https://www.rust-lang.org/) >= 1.94 (MSRV)
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

# Rust 编译检查
cd src-tauri && cargo check

# Rust 测试
cd src-tauri && cargo test
```

### 环境变量

```bash
# .env.example
VITE_API_BACK_URL=http://localhost:8080  # Python 后端 URL
VITE_APP_NAME=EMA AI Agent               # 应用显示名称
EMA_PROJECT_ROOT=..                       # 项目根目录（用于 Python 后端自动启动）
EMA_AUTO_START_BACKEND=true               # 随 Tauri 应用自动启动 Python 后端
```

### 添加新页面/组件

1. 在 `app/pages/` 下创建 `.vue` 文件 — Nuxt 4 自动注册路由
2. 在 `app/components/` 下创建组件 — 自动全局可用
3. 在 `app/composables/` 下创建组合式逻辑
4. 在 `app/assets/ts/tailwind.config.ts` 中添加自定义 token
5. 在 `app/i18n/locales/zh.json` 和 `en.json` 中添加 i18n 键

### 启动 Python 后端

```bash
# 从项目根目录 (EMA_AI_agent/)
python -m server
```

后端默认在 `http://127.0.0.1:8080` 启动（可通过 `VITE_API_BACK_URL` 配置）。

### 添加新 IPC 命令

1. 在 `src-tauri/src/commands/<module>.rs` 定义请求/响应类型，加 `#[derive(TS)]`
2. 实现 `#[tauri::command]` 函数，使用 `PythonBridge` 方法
3. 在 `lib.rs` 的 `.invoke_handler(tauri::generate_handler![...])` 中注册
4. 运行 `cargo test` 重新生成 TypeScript 类型到 `app/types/backend/`
5. 在 `app/composables/bridge.ts` 中添加对应封装

---

## 许可证

随 EMA AI Agent 主项目许可证。
