# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## 概述

`client` 是 EMA AI Agent 的前端，一个与 [server/](../server/) 中的 Python 后端（Robyn，默认 `http://127.0.0.1:8080`）通信的**流式 SPA 聊天客户端**。

基于 **Tauri 2 + Nuxt 4（Vue 3 + TypeScript）** 构建：

- **流式聊天** — 智能体响应通过 WebSocket（`/sessions/agent/ws`）以带类型的分块（`text` / `reasoning` / `tool_start` / `tool_end` / `tool_result`）流式传输，并支持 HITL（人机协同）审批卡片
- **离线优先的历史记录** — Dexie.js（IndexedDB）缓存会话历史、子智能体运行记录、角色档案和聊天背景图片；历史请求缓存优先，只拉取缺失的轮次
- **丰富的工具 UI** — 技能管理器、定时任务、心跳编辑器、通道设置、日志查看器（服务端 + 客户端日志）、统计图表、子智能体流程图、知识图谱查看器
- **深色/浅色模式 + 国际化** — `@nuxtjs/color-mode` 主题与 4 种语言（`zh` / `en` / `ja` / `ko`）
- **双模式桥接** — `bridge.ts` 自动检测 Tauri 桌面环境与普通浏览器，并选择对应的传输方式

> **开发状态**：积极开发中。浏览器代码路径（直连 Python 后端的 HTTP/WS）已完整实现；`src-tauri/` 的 Rust 侧目前仅包含占位模块（见[架构](#架构)）。

---

## 架构

### 混合架构

```
+--------------------------------------------------------------+
|                前端（Nuxt 4 SPA，app/）                       |
|  Vue 3 组件 + composables + Pinia + mitt 事件总线             |
+---------------------------+----------------------------------+
                            |  bridge.ts（运行时自动检测）
              +-------------+--------------+
              |                            |
   [浏览器模式]                  [Tauri 模式]
   ofetch HTTP REST +            invoke() IPC 命令
   原生 WebSocket                + Tauri Events（agent:stream:*）
              |                            |
              |                 src-tauri/（Rust 外壳，目前
              |                 仅含占位模块）
              |                            |
              +-------------+--------------+
                            |
              HTTP / WS  http://localhost:8080
              （VITE_API_BACK_URL，无开发代理）
                            |
+---------------------------v----------------------------------+
|              Python 后端（server/，Robyn）                    |
|  REST 端点 + WebSocket 端点                                   |
|  Agent Core (LangGraph) | Memory | Skills | Cron | Channels  |
+--------------------------------------------------------------+
```

### 数据流

```
用户交互（Vue 组件）
    -> bridge.ts（自动检测 Tauri / 浏览器模式）
        |
        |--> [浏览器模式] fetchApi() REST + WebSocket /sessions/agent/ws
        |        （Base64 媒体先经 POST /images|/audio|/video/upload 上传）
        |
        |--> [Tauri 模式] invoke() -> Rust IPC 命令 -> Tauri Events
        |
    -> 响应式状态（composables + Pinia + mitt 事件总线）
    -> 响应式 UI 更新
```

**关于 Tauri 模式的说明**：`bridge.ts` 仍保留了完整的 Tauri IPC 代码路径（`agent_chat`、`agent_stop`、`session_clear`、`session_history`、`system_prompt_*`、`memory_*`、`system_health`、`subagent_runs`、`subagent_run_delete`），与 `app/types/backend/*`（ts-rs 生成）一一对应。但 `src-tauri/src/` 目前**仅包含空的占位模块**（`config/`、`core/`、`database/`、`prompts/`、`rag/`、`runtime/`、`sessions/`、`skills/`、`tools/`、`types/` —— 各为一个空的 `mod.rs`）；当前代码树中不存在 Rust 入口与 IPC 命令实现。此外，若干流程（心跳、定时任务、技能、通道、curator、日志、`/env`、子智能体操控）完全绕过 Rust，在两种模式下都始终走 `fetchApi`。

---

## 目录结构

```
client/
├── .env.example                   # 环境变量模板（VITE_APP_NAME、VITE_API_BACK_URL）
├── eslint.config.mjs              # ESLint flat 配置
├── nuxt.config.ts                 # Nuxt 4 配置（SPA、Tailwind v4、i18n、PrimeVue、Pinia、color-mode）
├── package.json                   # 依赖清单（脚本：dev/build/generate/preview/typecheck/test/...）
├── playwright.config.ts           # Playwright repro 配置（./repros、Desktop Edge、localhost:3000）
├── pnpm-lock.yaml                 # pnpm 锁文件
├── pnpm-workspace.yaml            # pnpm 工作区配置（allowBuilds）
├── prettier.config.mjs            # Prettier 配置
├── tsconfig.json                  # TypeScript 配置
├── vitest.config.ts               # Vitest 单元测试配置（composables、happy-dom、v8 覆盖率）
├── vitest.integration.config.ts   # Vitest 集成测试配置（真实 .vue SFC、mock 后端）
├── app/                           # Nuxt 4 SPA 源码
│   ├── app.vue                    # 根组件 —— 布局、Toast 层、连接横幅、语言恢复
│   ├── common.scss                # 300+ 行全局 SCSS mixin 库（布局、形状、滚动条、……）
│   ├── assets/css/
│   │   ├── main.css               # Tailwind v4 入口（@import 'tailwindcss'）+ @theme 令牌 + 重置样式
│   │   └── main.scss              # 全局 SCSS 入口（在 nuxt.config css 中加载）
│   ├── common/utils.ts            # 共享工具函数（dayjs 配置、formatCompactTimeString、……）
│   ├── components/
│   │   ├── chat/inputBox.vue      # 聊天输入框组件（支持 i18n）
│   │   └── ImagePreviewOverlay.vue# 全屏图片预览浮层（Teleport 到 body）
│   ├── composables/               # Vue 3 组合式逻辑（单元测试位于 __tests__/）
│   │   ├── bridge.ts              # 统一的 Tauri/Browser 桥接 —— 聊天流、会话、系统提示词、
│   │   │                          #   记忆、心跳、定时任务、技能、通道、curator、日志、env
│   │   ├── requestApi.ts          # fetchApi HTTP 封装（ofetch，baseURL 取自 VITE_API_BACK_URL）
│   │   ├── ws.ts                  # WebSocket 单例：/sessions/ws + /subagents/ws（5 秒自动重连）
│   │   ├── db.ts                  # Dexie（IndexedDB）缓存：消息、角色档案、背景图、运行记录
│   │   ├── messages.ts            # 历史接口（缓存优先的 /get_history_by_turn_page）+ 流中断事件
│   │   ├── connection.ts          # 后端健康轮询（5 秒）+ 上下线 Toast
│   │   ├── toast.ts               # 全局 Toast 层（PrimeVue Toast 注册）
│   │   ├── clientLog.ts           # 客户端 console.* 捕获并持久化到 Dexie（all/log/error）
│   │   ├── env.ts                 # 读写后端 .env（GET/PUT /env）
│   │   ├── workspace.ts           # 系统提示词处理器（/system_prompt GET/POST/PATCH）
│   │   ├── defaultCharacter.ts    # 内置默认角色（名称 + /avatar/*.jpg）
│   │   ├── sessionFilter.ts       # 客户端会话列表过滤（关键词 + 日期范围）
│   │   ├── useChatBackground.ts   # 全局聊天背景图片（Dexie 持久化的单例）
│   │   ├── useImagePreview.ts     # 图片预览浮层状态
│   │   ├── useSubagentTasks.ts    # 后台任务状态单例（fetch + WS + Dexie）
│   │   ├── utils.ts               # max/min + 日期时间工具
│   │   ├── mitt.ts                # mitt 事件总线实例
│   │   └── system.ts              #（空占位文件）
│   ├── declare/declarations.d.ts  # 类型声明
│   ├── i18n/locales/              # en.json / ja.json / ko.json / zh.json
│   ├── layouts/default.vue        # 默认布局 —— 全屏视图容器
│   ├── pages/
│   │   ├── index.vue              # 渲染 ChatInputBox（路由 / 经 routeRules 301 重定向到 /home）
│   │   ├── knowledge-graph/
│   │   │   └── index.vue          # 知识图谱查看器（@antv/g6、文档上传、开发中）
│   │   └── home/
│   │       ├── index.vue          # 主聊天外壳 —— SessionSidebar + 工具栏 + 嵌套 NuxtPage
│   │       ├── config.ts          # 工具栏（图片/音频/视频上传）与头部工具定义
│   │       ├── type.ts            # SessionRecord / Tool / MessageItem 类型定义
│   │       ├── index/[sid].vue    # 单会话聊天页（KeepAlive、HITL 卡片、任务跳转栏）
│   │       ├── index/tasks/[sid].vue  # 独立后台任务页（/home/tasks/{sid}）
│   │       └── components/        # 20 个页面组件：
│   │           ├── ChatBox.vue            # 消息列表（markdown-it + DOMPurify，媒体经 /media）
│   │           ├── SessionSidebar.vue     # 会话列表侧边栏（新建/重命名/过滤会话）
│   │           ├── HistoryItem.vue        # 侧边栏历史会话条目
│   │           ├── ModeSwitch.vue         # 深色/浅色切换（PrimeVue ToggleSwitch）
│   │           ├── ExtendDialog.vue       # "Extend" 对话框
│   │           ├── ConfigDialog.vue       # 系统配置（.env 编辑器、背景、语言、……）
│   │           ├── PersonaDialog.vue      # 系统提示词 / 人格编辑器
│   │           ├── MemoryDialog.vue       # 长期记忆编辑器（workspace/memory/*）
│   │           ├── HeartbeatDialog.vue    # HEARTBEAT.md 编辑器
│   │           ├── CronDialog.vue         # 定时任务管理（/cron）
│   │           ├── SkillsDialog.vue       # 技能管理器（列表/上传/启停/置顶/删除/curator）
│   │           ├── ChannelSettingsDialog.vue  # 通道开关与单通道配置
│   │           ├── LogsDialog.vue         # 日志查看器（服务端日志 + 客户端日志，实时流）
│   │           ├── NotificationDialog.vue # 服务端推送通知列表
│   │           ├── StatsDialog.vue        # 使用统计（@antv/g2，经 GChart.vue）
│   │           ├── GChart.vue             # @antv/g2 图表封装
│   │           ├── SubagentTasksView.vue  # 后台任务视图（列表/详情/流程图）
│   │           ├── SubagentRunDetail.vue  # 单个子智能体运行详情
│   │           ├── SubagentFlowGraph.vue  # 子智能体运行树图（@antv/g6）
│   │           └── AvatarCropDialog.vue   # 头像上传 + 裁剪（cropperjs）
│   ├── stores/ui.ts               # Pinia UI store（sidebarCollapsed 持久化到 localStorage）
│   └── types/
│       ├── message.ts             # BaseMessage / AiMessage / MultiModalMessage、……
│       ├── response.d.ts          # API 响应类型定义
│       └── backend/               # ts-rs 生成的后端类型（ChatRequest、HealthStatus、……）
├── docs/                          # VitePress 文档站点（guide/commands/events/types/zh）
├── public/                        # 静态资源（favicon.svg、robots.txt、avatar/）
├── repros/                        # Playwright repro 测试（经 playwright.config.ts 运行）
├── src-tauri/                     # Tauri 2 原生外壳
│   ├── capabilities/default.json  # 权限（core:default、shell:*、notification、
│   │                              #   global-shortcut:default、window-state:default）
│   ├── icons/                     # 应用图标
│   ├── resources/                 # 打包资源占位（skills/、templates/）
│   ├── src/                       # 仅含空占位模块：config/ core/ database/
│   │                              #   prompts/ rag/ runtime/ sessions/ skills/ tools/ types/
│   ├── tests/                     # Rust 测试占位（空 mod.rs + .gitkeep 目录）
│   ├── benches/                   # 基准测试占位
│   ├── .env.example               # 后端模型配置模板（MAIN_LLM_*、TAVILY_API_KEY、……）
│   ├── Cargo.toml                 # Rust 清单（tauri 2、reqwest、ts-rs、tracing、插件、……）
│   ├── Cargo.lock                 # Rust 锁文件
│   ├── build.rs                   # Tauri 构建脚本
│   └── tauri.conf.json            # Tauri 2 配置（beforeDev: pnpm dev，beforeBuild: pnpm build）
└── tests/integration/             # Vitest 集成测试（真实 SFC、mock 后端）
```

## 技术栈

| 层 | 技术 | 用途 |
|-------|-----------|---------|
| **跨平台外壳** | [Tauri 2](https://v2.tauri.app/)（`2.0.0-rc.17`、`@tauri-apps/api` ^2.11.1、`@tauri-apps/cli` 2.11.4） | 原生桌面打包 + 配置/能力/图标 |
| **前端框架** | [Nuxt 4](https://nuxt.com/) ^4.5.2 + [Vue 3](https://vuejs.org/) ^3.5.41 | SPA 模式（`ssr: false`）、Composition API + `<script setup lang="ts">` |
| **UI 组件** | [PrimeVue](https://primevue.org/) ^4.5.0 + PrimeIcons ^8.0.0（`@primevue/nuxt-module`） | Dialog、Button、Select、ToggleSwitch、Toast 等 |
| **状态管理** | [Pinia](https://pinia.vuejs.org/) ^4.0.3（`@pinia/nuxt`）+ `pinia-plugin-persistedstate`（localStorage） | 全局 UI 状态（侧边栏、主题入口） |
| **样式** | [Tailwind CSS](https://tailwindcss.com/) v4（经 `@tailwindcss/vite`）+ SCSS（`sass`） | 原子化 CSS + `@theme` 设计令牌 + SCSS mixin 库 |
| **颜色模式** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | 深色/浅色主题切换（`.dark` 类） |
| **国际化** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) 10.6.0 | zh / en / ja / ko，`no_prefix` 策略 |
| **Markdown 渲染** | [markdown-it](https://github.com/markdown-it/markdown-it) ^15 | 聊天消息 markdown → HTML（ChatBox.vue） |
| **XSS 防护** | [DOMPurify](https://github.com/cure53/DOMPurify) ^3.4 | 净化渲染后的 HTML |
| **日期格式化** | [dayjs](https://day.js.org/) | 紧凑时间戳（`YYYYMMDDHHmmss`）解析/格式化 |
| **离线存储** | [Dexie.js](https://dexie.org/) ^4.4.4 | IndexedDB 封装：消息、角色、背景图、子智能体运行、客户端日志 |
| **事件总线** | [mitt](https://github.com/developit/mitt) ^3 | 轻量跨组件通信 |
| **图表 / 图谱** | [@antv/g2](https://g2.antv.antgroup.com/) ^5、[@antv/g6](https://g6.antv.antgroup.com/) ^5 | 统计图表、子智能体流程图、知识图谱 |
| **图片裁剪** | [cropperjs](https://github.com/fengyuanchen/cropperjs) ^1.6 | 头像上传与裁剪 |
| **工具库** | [lodash-es](https://lodash.com/) ^4.18 | 常用工具函数 |
| **单元 / 集成测试** | [Vitest](https://vitest.dev/) ^4 + happy-dom + @vue/test-utils + @vitest/coverage-v8 | composable 单元测试 + SFC 集成测试 |
| **E2E repro** | [@playwright/test](https://playwright.dev/) ^1.62 | 面向开发服务器的 repro 测试（Desktop Edge） |
| **Lint / 格式化** | ESLint ^10（flat config）+ Prettier | 代码质量 |
| **类型检查** | [vue-tsc](https://github.com/vuejs/language-tools) ^3.3.9 | `pnpm typecheck` |
| **后端语言** | [Rust](https://www.rust-lang.org/) 2021 edition（MSRV 1.94） | Tauri 外壳（src-tauri/，目前为占位模块） |

### 关键配置

- **Nuxt**（`nuxt.config.ts`）：`ssr: false`（纯 SPA）；`devtools` 已禁用；Vite 配置 `clearScreen: false`、`envPrefix: ['VITE_', 'TAURI_']`、`server.strictPort: true`（Tauri 需要固定端口）；CSS 入口 `~/assets/css/main.css` + `~/assets/css/main.scss`；路由规则 `/` → 301 重定向到 `/home`；`src-tauri/` 已排除扫描
- **Tauri**（`tauri.conf.json`）：产品名 "EMA AI Agent"、版本 0.1.0、标识符 `com.ema-ai.agent`、`beforeDevCommand: pnpm dev`、`beforeBuildCommand: pnpm build`、开发 URL `http://localhost:3000`、frontendDist `../dist`、CSP `null`、主窗口 800×600 可调整大小
- **Tailwind**（v4）：经 `@tailwindcss/vite` 加载；入口 `app/assets/css/main.css` 引入 `tailwindcss` 与 PrimeIcons；`@custom-variant dark` 由 `.dark` 类触发；自定义 `@theme` 令牌（断点 480/768/976/1440，颜色 `gray-dark`/`gray-light`/`theme-main`，衬线字体 Merriweather，z-index 1–3）
- **i18n**：策略 `no_prefix`、`defaultLocale: 'en'`、语言 `zh`/`en`/`ja`/`ko`、`detectBrowserLanguage: false`；`app.vue` 在挂载时恢复语言（偏好 cookie `i18n_redirected` → 浏览器语言 → 回退 `en`）
- **PrimeVue**：由 Aura 预设派生的自定义 `NoirPreset`（slate 主色板）；通过 `.dark` 选择器切换深色模式
- **Pinia 持久化**：全局配置 `pinia-plugin-persistedstate`，`storage: 'localStorage'`

---

## 前端架构

### 组件层级

```
app.vue（根：Toast 层、连接横幅、语言恢复）
  └─ NuxtLayout（layouts/default.vue）
       └─ NuxtPage
            ├─ /            → 301 重定向到 /home（routeRules）
            ├─ /knowledge-graph  （knowledge-graph/index.vue，@antv/g6）
            └─ /home（home/index.vue：SessionSidebar + 工具栏）
                 └─ NuxtPage（page-key = route.params.sid，KeepAlive）
                      ├─ /home/{sid}       （index/[sid].vue —— 聊天 + HITL 卡片）
                      └─ /home/tasks/{sid} （index/tasks/[sid].vue —— SubagentTasksView）
```

### 通信桥接（bridge.ts）

`bridge.ts` 提供在 Tauri 桌面与浏览器两种模式下均可工作的统一 API。所有后端访问都经由它（或经 `requestApi.ts` 中的 `fetchApi`）：

| API | 说明 |
|-----|-------------|
| `streamChatMessage(request, onChunk, onHitl?, onDone?)` | 流式智能体聊天；返回 `{ controller, promise }`（Tauri Events 或 `/sessions/agent/ws`） |
| `sendChatMessage(request, onChunk)` | `streamChatMessage` 的便捷封装 |
| `stopChatMessage(sessionId)` | 停止正在进行的生成（`agent_stop` IPC 或 WS `stop` 帧） |
| `resumeHitl(sessionId, decision, ...)` | 通过新建 WebSocket 恢复暂停的 HITL 智能体 |
| `clearSession(sessionId)` | 清空会话状态（`session_clear` IPC 或 `DELETE /sessions`） |
| `getHistory(sessionId, lastTurnCount)` | 获取历史（`session_history` IPC 或 `GET /n_turns_history_messages`） |
| `fetchSubagentRuns` / `fetchSubagentRunSubtree` / `deleteSubagentRunSubtree` / `steerSubagentRun` | 后台子智能体任务管理 |
| `readSystemPrompt` / `writeSystemPrompt` / `updateSystemPrompt` / `readSystemPromptTemplate` | 系统提示词文件 CRUD |
| `readMemory` / `writeMemory` | 长期记忆文件（`workspace/memory/*`） |
| `readHeartbeat` / `writeHeartbeat` | `workspace/HEARTBEAT.md`（始终走 HTTP） |
| `listCronJobs` / `addCronJob` / `updateCronJob` / `runCronJob` / `enableCronJob` / `deleteCronJob` | 定时任务管理 |
| `listSkills` / `readSkill` / `uploadSkill` / `setSkillActive` / `deleteSkill` / `pinSkill` | 技能管理 |
| `listChannels` / `updateChannel` / `getChannelConfig` / `updateChannelConfig` | 通道设置 |
| `runCuratorReview` / `getCuratorSettings` / `setCuratorSettings` | 自动技能 curator 控制 |
| `listLogFiles` / `readLogFile` / `openLogStream` | 后端日志读取 + 实时 `/logs/ws` 流 |
| `readEnvConfig` / `writeEnvConfig`（`env.ts`） | 后端 `.env` 读取/更新（`GET/PUT /env`） |
| `checkHealth()` | 后端可达性（`system_health` IPC 或 `GET /system_prompt`） |

浏览器模式聊天流细节：

- 连接 `ws(s)://{VITE_API_BACK_URL}/sessions/agent/ws` 并发送 `{ session_id, multi_modal_message }`
- Base64 媒体先经 `POST /images/upload`、`/audio/upload`、`/video/upload` 上传并以 URL 引用
- 服务端帧：`{ event: "chunk" | "done" | "error" | "stopped" | "hitl_request", ... }`；chunk 携带 `type`（`text`/`reasoning`/`tool_start`/`tool_end`/`tool_result`）与工具元数据
- 流中断后按指数退避重连（1s/2s/4s，经 `WS_RECONNECT_MAX_ATTEMPTS` 最多 3 次）；流中丢失会抛出 `StreamInterruptedError`，并经 mitt 发出 `ws:conn-loss` / `stream:reconnecting` / `stream:reconnected` / `stream:reconnect:failed`
- HITL 中断携带 `HitlInterruptData`（工具名/参数/可选决定）；决定以 `hitl_response` 帧发送（`approve` / `reject` / `edit`）

### WebSocket 单例（ws.ts）

两个相互独立的模块级单例连接（均在 5 秒后自动重连）：

| 连接 | 端点 | 经 mitt 派发的事件 |
|-----------|----------|-----------------|
| 会话推送 | `/sessions/ws?session_id=default` | `ws:connected`、`ws:notification`、`ws:message`、`ws:disconnected` |
| 子智能体推送 | `/subagents/ws` | `ws:subagents:connected`、`ws:subagents:ready`、`ws:subagent_spawned`、`ws:subagent_ended`、`ws:subagents:message`、`ws:subagents:disconnected` |

两者均从 `VITE_API_BACK_URL` 解析基础 URL（`http://` → `ws://`，`https://` → `wss://`）。

### 客户端使用的后端端点

REST（基础 URL `VITE_API_BACK_URL`，默认 `http://localhost:8080`）：

| 端点 | 方法 | 用途 |
|----------|-----------|---------|
| `/sessions` | DELETE | 清空会话 |
| `/n_turns_history_messages` | GET | 最近 N 轮历史 |
| `/get_history_by_turn_page` | GET | 分页历史（经 Dexie 缓存优先） |
| `/sessions/agent/ws` | WS | 聊天流、停止、HITL 恢复 |
| `/sessions/ws` | WS | 服务端推送通知 |
| `/subagents/ws` | WS | 子智能体生成/结束推送 |
| `/subagents/runs` | GET / DELETE | 子智能体运行记录（列表/子树/删除） |
| `/subagents/steer` | POST | 操控/恢复子智能体运行 |
| `/system_prompt` | GET / POST / PATCH / PUT | 系统提示词读取/写入/更新 |
| `/system_prompt/template` | GET | 人格模板文件 |
| `/memory` | GET / PUT | 长期记忆文件 |
| `/heartbeat` | GET / PUT | HEARTBEAT.md |
| `/cron`、`/cron/trigger`、`/cron/enable` | GET/POST/PUT/DELETE | 定时任务 CRUD + 触发 |
| `/skills`、`/skills/{path}`、`/skills/upload`、`/skills/toggle`、`/skills/delete`、`/skills/pin` | GET/POST | 技能管理 |
| `/curator/run`、`/curator/settings` | POST / GET / PUT | Curator 审查与设置 |
| `/channels`、`/channels/{name}`、`/channels/{name}/config` | GET / PUT | 通道开关与配置 |
| `/env` | GET / PUT | 后端 `.env` 读取/更新 |
| `/logs/files`、`/logs` | GET | 日志文件列表与尾部读取 |
| `/logs/ws` | WS | 实时日志流 |
| `/images/upload`、`/audio/upload`、`/video/upload` | POST | Base64 媒体上传 → URL |
| `/media` | GET | 渲染持久化的媒体文件 |

---

## 核心模块细节

### SCSS Mixin 库（`common.scss`）

一个 300+ 行的 SCSS mixin 库，为布局、形状、滚动条与文本溢出提供工具（如 `fullViewWindow`、`flexCenter`、`scrollBar`、`wordEllipsis`），被页面、组件与布局广泛使用。

### 离线缓存（db.ts，Dexie/IndexedDB）

- `CachedMessage` —— 镜像后端消息表行（turn_num、images/audios/videos、tool 字段、token 计数）；历史请求缓存优先，只拉取比缓存最大轮次更新的部分
- `CachedCharacter` —— 按会话缓存的头像/名称快照（base64 data URL 或来自 `public/avatar/` 的 `/avatar/*.jpg`）
- 从 `/subagents/ws` 推送与 REST 拉取缓存的子智能体运行记录
- 聊天背景图片配置（全局，经 `useChatBackground` 的模块级单例）
- `clientLog.ts` 将捕获的浏览器 `console.*` 输出持久化到 `all` / `log` / `error` 分桶结构

### 状态与事件

- **Pinia**（`stores/ui.ts`）：侧边栏折叠（持久化）、设置菜单标志、主题写入入口
- **mitt 事件总线**：WS 事件、流重连事件、会话流中断（`session:abort-stream`）、跨组件通知
- **connection.ts**：每 5 秒轮询 `checkHealth()`；暴露 `isOnline` / `backendStatus` 并驱动 `app.vue` 的全局连接横幅

### 类型生成（app/types/backend/）

由 [ts-rs](https://github.com/Aleph-Alpha/ts-rs) 从 Rust 后端结构体生成的 TypeScript 接口（`ChatRequest`、`HistoryMessage`、`HealthStatus`、`AgentStreamChunk`、`PromptFileResponse` 等）。文件头部带有 "Do not edit manually" 声明。

### 测试

- **单元测试**（`pnpm test`）：`app/**/*.{test,spec}.ts` —— `app/composables/__tests__/` 下 20+ 个 composable 测试套件（happy-dom，`vue-i18n` 已 stub）
- **集成测试**（`pnpm test:integration`）：`tests/integration/` —— 挂载真实 `.vue` SFC（ChatBox、HistoryItem、主页、ModeSwitch、inputBox、图片渲染），后端被 mock
- **Repro**（`repros/`）：针对 `localhost:3000` 上 Nuxt 开发服务器的 Playwright 用例（已安装 MS Edge channel）

---

## 开发指南

### 前置要求

- [Node.js](https://nodejs.org/)（LTS）
- [pnpm](https://pnpm.io/) —— 项目使用 `pnpm-lock.yaml` / `pnpm-workspace.yaml`；请使用 pnpm 以保持锁文件权威
- [Rust](https://www.rust-lang.org/) 工具链（MSRV 1.94）—— 仅在构建 `src-tauri/` 时需要
- 项目根目录的 Python 后端（见[根 README](../README.md)）

### 常用命令

```bash
# 安装依赖（pnpm 是包管理器；postinstall 会运行 `nuxt prepare`）
pnpm install

# 开发服务器（浏览器模式，Nuxt 开发服务器 http://localhost:3000）
pnpm dev

# 生产构建（SPA 输出到 dist/ —— 即 tauri.conf.json 的 frontendDist）
pnpm build

# 静态生成 / 预览
pnpm generate
pnpm preview

# 类型检查（vue-tsc --noEmit）
pnpm typecheck

# 单元测试（Vitest，happy-dom）
pnpm test
pnpm test:watch

# 集成测试（真实 SFC，mock 后端）
pnpm test:integration
pnpm test:integration:watch

# Tauri 桌面开发模式（先经 beforeDevCommand 运行 `pnpm dev`）
pnpm tauri dev

# Tauri 桌面构建（先经 beforeBuildCommand 运行 `pnpm build`）
pnpm tauri build
```

`pnpm tauri dev` / `pnpm tauri build` 调用 `@tauri-apps/cli` 开发依赖中的 Tauri CLI。

### 环境变量

```bash
# client/.env.example
VITE_APP_NAME="sherry"                     # 应用显示名（document <title>）
VITE_API_BACK_URL="http://localhost:8080"  # Python 后端基础 URL（REST + WS）
```

所有后端调用都会解析 `VITE_API_BACK_URL`，并硬编码回退到 `http://localhost:8080`。没有开发服务器代理 —— 前端直连后端，因此后端必须允许跨域请求（后端返回 `Access-Control-Allow-Origin: *`）。

`src-tauri/.env.example` 单独保存**后端**模型配置模板（`MAIN_LLM_*`、`REASONER_*`、`SIMPLE_MAIN_LLM_*`、`ITTT_*`、`TTI_*`、`RERANKER_*`、`EMBEDDING_*`、`TAVILY_API_KEY`、`LANGSMITH_*`）。

### 启动 Python 后端

在项目根目录（完整搭建见根 README）：

```bash
uv run python -m server
```

后端默认监听 `http://127.0.0.1:8080`（可通过 `VITE_API_BACK_URL` 在客户端侧修改）。

### 新增页面 / 组件

1. 在 `app/pages/` 下创建 `.vue` 文件 —— Nuxt 4 自动注册路由（`home/` 下的嵌套页面由 `home/index.vue` 的内层 `<NuxtPage>` 渲染）
2. 在 `app/components/` 或 `app/pages/home/components/` 下创建组件
3. 在 `app/composables/` 下添加组合式逻辑（单元测试放在 `app/composables/__tests__/`）
4. 在 `app/assets/css/main.css` 的 `@theme` 块中添加自定义令牌
5. 向全部四个语言文件添加 i18n 键：`app/i18n/locales/{en,ja,ko,zh}.json`

### 新增后端调用

1. 在 `app/composables/bridge.ts` 中添加端点调用（纯 REST 也可放在 `requestApi.ts`）——决定走 Tauri IPC 路径、`fetchApi` 还是两者兼用
2. 若后端出现新的载荷结构，扩展 `app/types/backend/` 或 `app/types/message.ts` 中的对应类型
3. 在 `app/composables/__tests__/` 中为新逻辑补充单元测试

---

## License

MIT —— 与 EMA AI Agent 主项目相同（见 `src-tauri/Cargo.toml` 的 `license` 字段）。
