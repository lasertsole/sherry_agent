# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## 概述

`client` 是 EMA AI Agent 的前端，一个与 [server/](../server/) 中的 Python 后端（Robyn，默认 `http://127.0.0.1:8080`）通信的**流式 SPA 聊天客户端**。

基于 **Tauri 2 + Nuxt 4（Vue 3 + TypeScript）** 构建：

- **流式聊天** — 智能体响应通过 WebSocket（`/sessions/agent/ws`）以带类型的分块（`text` / `reasoning` / `tool_start` / `tool_end` / `tool_result`）流式传输，并支持 HITL（人机协同）审批卡片
- **可折叠的回合执行历史** — 已完成的回合只显示执行摘要与最终回答；展开后恢复全部工具卡片、中间消息与思考过程
- **离线优先的历史记录** — Dexie.js（IndexedDB）缓存会话历史、子智能体运行记录、角色档案和聊天背景图片；历史请求缓存优先，只拉取缺失的轮次
- **丰富的工具 UI** — 技能管理器、定时任务、心跳编辑器、通道设置、日志查看器（服务端 + 客户端日志）、统计图表、子智能体流程图、知识图谱查看器
- **深色/浅色模式 + 国际化** — `@nuxtjs/color-mode` 主题与 4 种语言（`zh` / `en` / `ja` / `ko`）
- **双模式桥接** — `bridge.ts` 自动检测 Tauri 桌面环境与普通浏览器，并选择对应的传输方式

> **开发状态**：积极开发中。浏览器代码路径（直连 Python 后端的 HTTP/WS）已完整实现；`src-tauri/` 是桌面外壳——Python 后端的进程生命周期、首次启动下载，以及桥接层调用的 IPC 命令（见[架构](#架构)）。

---

## 架构

### 混合架构

```text
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
              |                 src-tauri/（Rust 外壳：后端
              |                 进程 + 首次启动下载）
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

```text
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

**关于 Tauri 模式的说明**：`bridge.ts` 保留了 Tauri IPC 代码路径（`agent_chat`、`agent_stop`、`session_clear`、`session_history`、`system_prompt_*`、`memory_*`、`system_health`、`subagent_runs`、`subagent_run_delete`），与 `app/types/backend/*`（ts-rs 生成）一一对应。`src-tauri/src/` 实现了桌面外壳：`services/python_process.rs` 启动 Python 后端并把 `SHERRY_HOME` 指向应用数据目录，`services/backend_downloader.rs` 在打包版首次启动时下载该后端（GitHub Release 资产 + Tauri 进度事件），`services/python_bridge.rs` 通过 HTTP 与后端通信，`commands/` 是 IPC 入口。此外，若干流程（心跳、定时任务、技能、通道、curator、日志、`/env`、子智能体操控）完全绕过 Rust，在两种模式下都始终走 `fetchApi`。

---

## 目录结构

```text
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
│   │   ├── chat/DiffPreview.vue   # 工具卡片内 patch_file / write_file 结果的彩色内联 diff
│   │   └── ImagePreviewOverlay.vue# 全屏图片预览浮层（Teleport 到 body）
│   ├── composables/               # Vue 3 组合式逻辑（单元测试位于 __tests__/）
│   │   ├── bridge.ts              # 统一的 Tauri/Browser 桥接 —— 聊天流、会话、系统提示词、
│   │   │                          #   记忆、心跳、定时任务、技能、通道、curator、日志、env
│   │   ├── requestApi.ts          # fetchApi HTTP 封装（ofetch，baseURL 取自 VITE_API_BACK_URL）
│   │   ├── ws.ts                  # WebSocket 单例：/sessions/ws + /subagents/ws（5 秒自动重连）
│   │   ├── db.ts                  # Dexie（IndexedDB）缓存：消息、角色档案、背景图、运行记录
│   │   ├── messages.ts            # 历史接口（缓存优先的 /get_history_by_turn_page）+ 流中断事件
│   │   ├── toast.ts               # 全局 Toast 层（PrimeVue Toast 注册）
│   │   ├── clientLog.ts           # 客户端 console.* 捕获并持久化到 Dexie（all/log/error）
│   │   ├── env.ts                 # 读写后端三个配置文件（.env / .models / .network）
│   │   ├── workspace.ts           # 系统提示词处理器（/system_prompt GET/POST/PATCH）
│   │   ├── defaultCharacter.ts    # 内置默认角色（名称 + /avatar/*.jpg 与 default.svg 占位头像）
│   │   ├── sessionFilter.ts       # 客户端会话列表过滤（关键词 + 日期范围）
│   │   ├── useImagePreview.ts     # 图片预览浮层状态
│   │   ├── useSubagentTasks.ts    # 后台任务薄封装（Pinia store + fetch/WS/Dexie 同步）
│   │   ├── utils.ts               # max/min + 日期时间工具
│   │   ├── mitt.ts                # mitt 事件总线实例
│   │   ├── ws-ticket.ts           # WebSocket 单次票据预取（惰性 import，避免 socket 层引入认证桥）
│   │   ├── use-auth-refresh.ts    # 会话主动续期（在 HttpOnly 访问 Cookie 过期前轮换）
│   │   └── system.ts              #（空占位文件）
│   ├── declare/declarations.d.ts  # 类型声明
│   ├── i18n/locales/              # en.json / ja.json / ko.json / zh.json
│   ├── layouts/default.vue        # 默认布局 —— 全屏视图容器
│   ├── pages/
│   │   ├── index.vue              # 渲染 ChatInputBox（路由 / 经 routeRules 301 重定向到 /home）
│   │   ├── login/index.vue        # 登录页（**只输密码**——单账户、无用户名；仅在网关要求登录时出现）
│   │   └── home/
│   │       ├── index.vue          # 主聊天外壳 —— SessionSidebar + 工具栏 + 嵌套 NuxtPage
│   │       ├── config.ts          # 媒体下拉条目与头部工具定义
│   │       ├── type.ts            # SessionRecord / Tool / MessageItem 类型定义
│   │       ├── index/[sid].vue    # 单会话聊天页（KeepAlive、HITL 卡片、任务跳转栏）
│   │       └── components/        # 61 个页面组件：
│   │           ├── ChatBox.vue                # 消息列表（markdown-it + DOMPurify，媒体经 /media；选中文字右键可引用）
│   │           ├── BackgroundTasksPanel.vue#          # 聊天区的后台命令条（terminal(run_in_background=True)）：状态、pid，以及不影响对话的单命令「终止」
│   │           ├── SideChatPanel.vue          # 辅助会话标签页：右键引用新建普通子会话，独立消息流 + HITL 审批
│   │           ├── ChatTurnScrubber.vue       # 历史消息左侧的悬浮穿梭器（定位最近 20 轮内任意用户消息）
│   │           ├── ProgressFloat.vue          # 会话上方的计划进度悬浮框（todo + TaskFlow 扁平步骤列表，默认折叠成胶囊、恒常显示，经 taskflow_updated 实时刷新）
│   │           ├── ThinkingToggle.vue         # 会话级思考开关（开关，或按模型声明档位（含关闭）的选择器，下一轮生效）
│   │           ├── ContextUsageButton.vue     # 上下文占用环 + 占比面板（消息 / 系统提示词 / 工具调用）
│   │           ├── SessionModelPicker.vue     # 会话级主模型选择器（模型配置 MAIN_LLM 档案，下一轮生效）
│   │           ├── ThinkingCustomMappingPanel.vue # 会话级自定义思考 wire 映射（高级；适配内置格式未覆盖的 provider）
│   │           ├── MediaMenu.vue              # 输入栏的加号媒体下拉（图片 / 音频 / 视频）
│   │           ├── AccessModePicker.vue       # 工具栏访问模式：触发（计划模式 / 变更前确认 / 自动编辑 / 完全访问），下一个工具调用起生效
│   │           ├── ToolbarPopover.vue         # 工具栏入口的向上弹层（上下文环 / 运行中任务共用）
│   │           ├── TasksButton.vue            # 工具栏终端入口：本会话运行中的 subagent / 命令，弹框里点某项交给页面跳转（subagent 打开其实时任务视图，命令滚到它的工具卡）
│   │           ├── ProjectDirectoryChip.vue   # 会话项目目录的工具栏条目（绑定 / 清除，带续延与拒绝提示）
│   │           ├── ProjectDirectoryPicker.vue # 浏览器版内置文件夹选择器（GET /system/dirs；桌面版调系统对话框）
│   │           ├── SessionSidebar.vue         # 会话列表侧边栏（新建/重命名/过滤会话）；右边缘可拖拽（下限 280px、上限视口一半、宽度持久化）
│   │           ├── HistoryItem.vue            # 侧边栏历史会话条目
│   │           ├── ProjectFileTree.vue        # 左侧栏的项目文件树（只读，GET /project/tree，逐层懒加载；行右键菜单：打开 / 打开方式 / 用内置浏览器打开 / 在文件管理器中显示 / 复制绝对+相对路径 / 添加到聊天）
│   │           ├── GitGraphPanel.vue          # 左侧栏的 Git Graph（GET /git/graph，提交泳道用 SVG 绘制；标题栏分支名可切换分支，行右键可回退 / 切换，点击行展开该提交的文件清单）
│   │           ├── GitDiffPanel.vue          # 右栏的提交差异（左右两栏，红=删除 / 绿=新增；新增或删除的文件只有一栏）
│   │           ├── ContextViewerPanel.vue    # 右栏的 Agent 上下文（GET /context/inspect：系统提示词 / 工具定义 / 实时消息队列，打开时自动刷新）
│   │           ├── TrajectoryPanel.vue       # 当前会话 tab：按回合的轨迹账本（GET /sessions/:id/trajectory，按 turn 分组、客户端重排与分页）
│   │           ├── BrowserPanel.vue          # 工具箱浏览器：地址栏 + 内嵌页面（按会话记录历史，后退 / 前进 / 刷新；「自由尺寸」渲染可拖拽的设备框，边界与 ZCode 相同；「打开调试工具」在新窗口打开该页——iframe 无法内嵌开发者工具）。开启 agent 浏览器功能后切换为 CDP 模式：后端托管无头 Chromium，面板显示 screencast 帧并转发指针 / 键盘 / 滚轮事件（地址栏与导航按钮走 /browser/ws；「打开调试工具」打开真正的检查器页面，面板可直接观看）。地址栏还会联想持久化的访问历史（Dexie，跨重启保留），按 URL / 标题模糊匹配
│   │           ├── BrowserHistoryDropdown.vue # 地址栏的历史下拉：最近/匹配的访问记录（favicon、标题、相对时间）与清空操作
│   │           ├── TerminalPanel.vue         # 工具箱终端：由用户输入的命令，在会话的项目目录下执行
│   │           ├── FileTreeNode.vue           # 项目文件树的一行（展开 / 打开文件；可拖拽进输入框——落下即插入 @./path 引用 chip——行右键菜单在此接线）
│   │           ├── ModeSwitch.vue             # 深色/浅色切换（PrimeVue ToggleSwitch）
│   │           ├── ExtendPanel.vue            # 「扩展」标签页（通道 / MCP）
│   │           ├── ConfigPanel.vue            # 系统配置标签页（模型配置编辑——把 .models / .network / .env 合并成一个页签——背景图片、模型配置、应用配置、Token 预算、国际化）
│   │           ├── LlmModelManager.vue        # 模型配置的模型档案（分组面板、列表 / 编辑 / 新增；新增档案不含任何默认值，「测试连通性」经 POST /model/test 试调且不写 .models）
│   │           ├── LlmProfileRow.vue          # LlmModelManager 中的单条模型档案行（选择 / 编辑 / 删除）
│   │           ├── PersonaPanel.vue           # 系统提示词人设编辑面板——「预设角色」（角色配置 / 运行守则 / 人格灵魂 / 用户信息 / 工具 / 中间件 / 代理模型 / 技能 + 预设管理）
│   │           ├── SessionPresetButton.vue    # 顶栏只读入口：打开当前会话的预设标签页
│   │           ├── SessionPresetPanel.vue     # 「当前会话」标签页：本会话自己的预设，只读（代理模型排在第一页）
│   │           ├── NewSessionPresetDialog.vue # 每次「新建对话」必选的预设选择对话框（先应用预设，再创建会话）
│   │           ├── MemoryPanel.vue            # 长期记忆标签页（workspace/memory/*）
│   │           ├── HeartbeatPanel.vue         # HEARTBEAT.md 标签页 + 全局心跳开关
│   │           ├── CronPanel.vue              # 定时任务标签页（/cron）
│   │           ├── WebSearchPanel.vue         #「模型联网」标签页：会话的搜索后端（Tavily / SearXNG 单选）+ Tavily 密钥与 SearXNG 连接表单——这些 .network 键的唯一编辑入口（GET /network-config 已隐藏它们）
│   │           ├── SkillsPanel.vue            # 技能管理标签页（列表/上传/启停/置顶/删除/curator）
│   │           ├── ChannelSettingsDialog.vue  # 通道开关与单通道配置
│   │           ├── NotificationPanel.vue      # 服务端推送通知标签页（状态在 store 中，标签页关闭时徽标仍计数）
│   │           ├── AccountSettingsPanel.vue   # 账户标签页：设置 / 更换密码 / 关闭登录保护（需当前密码）
│   │           ├── I18nPanel.vue               # 国际化 tab 内容，嵌在系统配置面板的最后一个页签
│   │           ├── RightSidebar.vue           # 可折叠右侧栏 —— 所有工具都以标签面板承载（查看器 + 设置编辑器）；左边缘可拖拽（下限 280px，上限让对话区保住 320px / 35% 的下限，窗口挤不下所有下限时先缩到下限）
│   │           ├── FileViewerPanel.vue        # 「当前会话」下的文件查看标签页（GET /project/file：文本 + markdown，图片/PDF/docx 经 GET /project/file/blob）
│   │           ├── PdfViewer.vue             # 文件查看器内的 PDF 预览（pdf.js，翻页 + 适应宽度，GET /project/file/blob）
│   │           ├── DocxViewer.vue            # 文件查看器内的 docx 预览（mammoth → HTML → DOMPurify，GET /project/file/blob）
│   │           ├── LogsPanel.vue              # 日志查看标签页（服务端日志 + 客户端日志，实时流）
│   │           ├── StatsPanel.vue             # 统计标签页（@antv/g2，经 GChart.vue）
│   │           ├── KnowledgeGraphPanel.vue    # 知识图谱标签页（@antv/g6、文档上传）
│   │           ├── GChart.vue                 # @antv/g2 图表封装
│   │           ├── SubagentTasksView.vue      # 后台任务视图（运行图 + 选中 run 的详情），由任务详情标签页承载
│   │           ├── SubagentTasksPanel.vue     # 右侧栏标签页：面板内的 SubagentTasksView（不带返回会话动作）
│   │           ├── SubagentRunDetail.vue      # 单个子智能体运行详情
│   │           ├── SubagentFlowGraph.vue      # 子智能体运行树图（@antv/g6）
│   │           ├── TokenBudgetPanel.vue      # 系统配置 tab：当前会话的 Token 上限、用量/成本与重置（GET/POST /sessions/budget）
│   │           ├── ReplayAllowlistEditor.vue # 系统配置：回放门白名单编辑器（GET/PUT /sherry-config/list/<key>，增删可执行文件名）
│   │           ├── CudaPackCard.vue          # 模型配置：可选 CUDA 加速包卡片（仅 Linux——检测、断点续传下载，然后重启后端）
│   │           ├── RecentClosedTabsMenu.vue  # 右栏标签条最左的控件：最近关闭的标签，两组（当前会话 / 全局）
│   │           └── AvatarCropDialog.vue       # 头像上传 + 裁剪（cropperjs）
│   ├── stores/                    # Pinia stores
│   │   ├── ui.ts               # UI 状态（sidebarCollapsed / todoDockCollapsed 持久化到 localStorage）
│   │   ├── subagent.ts         # 后台任务状态（运行 / 树 / 选择）+ 派生视图
│   │   ├── todo.ts             # 会话计划（todo）列表 + 停靠可见性
│   │   ├── right-sidebar.ts    # 右侧栏（折叠状态 / 宽度持久化、已打开的标签类型）
│   │   ├── git-diff.ts         # 提交差异目标（每个文件一个右栏标签页，按 MRU 限量）
│   │   ├── toolbox.ts          # 工具箱状态：浏览器地址历史与终端回滚日志（按会话保存）
│   │   ├── thinking.ts         # 会话级思考开关 / 档位（下一轮生效）
│   │   ├── session-model.ts    # 会话级主模型覆盖（下一轮生效）
│   │   ├── context-usage.ts    # 会话级上下文计量（窗口 / 上报的提示词大小 / 各部分）
│   │   ├── running-commands.ts # 会话内运行中的后台命令（运行中的工具行，且仅当会话正在生成时）
│   │   ├── access-mode.ts      # 会话级访问模式（计划模式 / 变更前确认 / 自动编辑 / 完全访问，与后端同步）
│   │   ├── llm-profiles.ts     # 模型配置（MAIN_LLM 分组）的模型档案，供选择器使用
│   │   ├── connection.ts       # 后端连通性（isOnline / backendStatus）+ 去重 Toast；socket 断开按「后端不可用」呈现（offline 提示只在没有 WS 结论时兜底），未登录时保持沉默
│   │   ├── auth.ts             # 登录会话状态（status / user / 401 单次续期重放 / 轮换被拒时 markSessionLost）
│   │   ├── project-directory.ts # 每会话项目目录（绑定 / 续延选择 / 生效根）
│   │   ├── file-viewer.ts      # 文件查看标签页（打开路径 + 内容缓存）
│   │   ├── new-session.ts      # 新建会话必选预设对话框状态（所有「新建对话」入口）
│   │   ├── notification.ts     # 通知列表 + 未读徽标（标签页关闭时仍保持计数）
│   │   ├── agent-config.ts     # 预设的 agent 配置（工具 / 中间件 / 代理模型 / 技能）按会话镜像；必需工具与必需技能锁定不可取消，任务与计划 / 子代理两组只支持整体勾选
│   │   ├── taskflow.ts         # TaskFlow 进度（每会话 flow + 扁平步骤，由 taskflow_updated 刷新）
│   │   ├── web-search.ts       # 每会话的搜索后端 + Tavily→SearXNG 降级通知
│   │   └── chat-background.ts  # 全局聊天背景图片（Dexie 持久化）
│   ├── plugins/                   # Nuxt 插件
│   │   ├── auth-guard.ts          # 全局登录守卫（每次导航按 /auth/status 决策；收到 auth:session-lost 直接跳 /login，探测无结论时绝不放逐 /login）
│   │   ├── client-log.ts          # 浏览器 console.* 捕获写入客户端日志
│   │   ├── directives.ts          # 注册 v-debounce / v-safe-html 指令
│   │   └── error-handler.ts       # 全局错误处理安装（首个路由挂载之前）
│   └── types/
│       ├── message.ts             # BaseMessage / AiMessage / MultiModalMessage、……
│       ├── response.d.ts          # API 响应类型定义
│       └── backend/               # ts-rs 生成的后端类型（ChatRequest、HealthStatus、……）
├── wiki/                          # VitePress 文档站点（guide/commands/events/types/zh）
├── public/                        # 静态资源（favicon.svg、robots.txt、avatar/）
├── repros/                        # Playwright repro 测试（经 playwright.config.ts 运行）
├── src-tauri/                     # Tauri 2 原生外壳
│   ├── capabilities/default.json  # 权限（core:default、shell:*、notification、
│   │                              #   global-shortcut:default、window-state:default）
│   ├── icons/                     # 应用图标
│   ├── resources/                 # 占位目录（skills/、templates/）——已不是打包资源
│   ├── src/                       # 桌面外壳：services/（python_process、backend_downloader、
│   │                              #   python_bridge）、commands/ —— 另有占位模块
│   ├── tests/                     # Rust 测试占位（空 mod.rs + .gitkeep 目录）
│   ├── benches/                   # 基准测试占位

│   ├── Cargo.toml                 # Rust 清单（tauri 2、reqwest、ts-rs、tracing、插件、……）
│   ├── Cargo.lock                 # Rust 锁文件
│   ├── build.rs                   # Tauri 构建脚本
│   └── tauri.conf.json            # Tauri 2 配置（beforeDev: pnpm dev，beforeBuild: pnpm build）
└── tests/integration/             # Vitest 集成测试（真实 SFC、mock 后端）
```

## 技术栈

| 层                  | 技术                                                                                                     | 用途                                                                   |
| ------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **跨平台外壳**      | [Tauri 2](https://v2.tauri.app/)（`2.0.0-rc.17`、`@tauri-apps/api` ^2.11.1、`@tauri-apps/cli` 2.11.4）   | 原生桌面打包 + 配置/能力/图标                                          |
| **前端框架**        | [Nuxt 4](https://nuxt.com/) ^4.5.2 + [Vue 3](https://vuejs.org/) ^3.5.41                                 | SPA 模式（`ssr: false`）、Composition API + `<script setup lang="ts">` |
| **UI 组件**         | [PrimeVue](https://primevue.org/) ^4.5.0 + PrimeIcons ^8.0.0（`@primevue/nuxt-module`）                  | Dialog、Button、Select、ToggleSwitch、Toast 等                         |
| **状态管理**        | [Pinia](https://pinia.vuejs.org/) ^4.0.3（`@pinia/nuxt`）+ `pinia-plugin-persistedstate`（localStorage） | 全局状态（UI、后台任务、计划、连通性、聊天背景）                       |
| **样式**            | [Tailwind CSS](https://tailwindcss.com/) v4（经 `@tailwindcss/vite`）+ SCSS（`sass`）                    | 原子化 CSS + `@theme` 设计令牌 + SCSS mixin 库                         |
| **颜色模式**        | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/)                                                     | 深色/浅色主题切换（`.dark` 类）                                        |
| **国际化**          | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) 10.6.0                                                          | zh / en / ja / ko，`no_prefix` 策略                                    |
| **Markdown 渲染**   | [markdown-it](https://github.com/markdown-it/markdown-it) ^15                                            | 聊天消息 markdown → HTML（ChatBox.vue）                                |
| **XSS 防护**        | [DOMPurify](https://github.com/cure53/DOMPurify) ^3.4                                                    | 净化渲染后的 HTML                                                      |
| **日期格式化**      | [dayjs](https://day.js.org/)                                                                             | 紧凑时间戳（`YYYYMMDDHHmmss`）解析/格式化                              |
| **离线存储**        | [Dexie.js](https://dexie.org/) ^4.4.4                                                                    | IndexedDB 封装：消息、角色、背景图、子智能体运行、客户端日志           |
| **事件总线**        | [mitt](https://github.com/developit/mitt) ^3                                                             | 轻量跨组件通信                                                         |
| **图表 / 图谱**     | [@antv/g2](https://g2.antv.antgroup.com/) ^5、[@antv/g6](https://g6.antv.antgroup.com/) ^5               | 统计图表、子智能体流程图、知识图谱                                     |
| **图片裁剪**        | [cropperjs](https://github.com/fengyuanchen/cropperjs) ^1.6                                              | 头像上传与裁剪                                                         |
| **工具库**          | [lodash-es](https://lodash.com/) ^4.18                                                                   | 常用工具函数                                                           |
| **单元 / 集成测试** | [Vitest](https://vitest.dev/) ^4 + happy-dom + @vue/test-utils + @vitest/coverage-v8                     | composable 单元测试 + SFC 集成测试                                     |
| **E2E repro**       | [@playwright/test](https://playwright.dev/) ^1.62                                                        | 面向开发服务器的 repro 测试（Desktop Edge）                            |
| **Lint / 格式化**   | ESLint ^10（flat config）+ Prettier                                                                      | 代码质量                                                               |
| **类型检查**        | [vue-tsc](https://github.com/vuejs/language-tools) ^3.3.9                                                | `pnpm typecheck`                                                       |
| **后端语言**        | [Rust](https://www.rust-lang.org/) 2021 edition（MSRV 1.94）                                             | Tauri 外壳（后端进程 + 首次启动下载）                                  |

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

```text
app.vue（根：Toast 层、连接横幅、语言恢复）
  └─ NuxtLayout（layouts/default.vue）
       └─ NuxtPage
            ├─ /            → 301 重定向到 /home（routeRules）
            └─ /home（home/index.vue：SessionSidebar + 工具栏）
                 └─ NuxtPage（page-key = route.params.sid，KeepAlive）
                      └─ /home/{sid}       （index/[sid].vue —— 聊天 + HITL 卡片）
```

### 通信桥接（bridge.ts）

`bridge.ts` 提供在 Tauri 桌面与浏览器两种模式下均可工作的统一 API。所有后端访问都经由它（或经 `requestApi.ts` 中的 `fetchApi`）：

| API                                                                                                                                    | 说明                                                                                   |
| -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `streamChatMessage(request, onChunk, onHitl?, onDone?)`                                                                                | 流式智能体聊天；返回 `{ controller, promise }`（Tauri Events 或 `/sessions/agent/ws`） |
| `sendChatMessage(request, onChunk)`                                                                                                    | `streamChatMessage` 的便捷封装                                                         |
| `stopChatMessage(sessionId)`                                                                                                           | 停止正在进行的生成（`agent_stop` IPC 或 WS `stop` 帧）                                 |
| `resumeHitl(sessionId, decision, ...)`                                                                                                 | 通过新建 WebSocket 恢复暂停的 HITL 智能体                                              |
| `clearSession(sessionId)`                                                                                                              | 清空会话状态（`session_clear` IPC 或 `DELETE /sessions`）                              |
| `getHistory(sessionId, lastTurnCount)`                                                                                                 | 获取历史（`session_history` IPC 或 `GET /n_turns_history_messages`）                   |
| `fetchSubagentRuns` / `fetchSubagentRunSubtree` / `deleteSubagentRunSubtree` / `steerSubagentRun`                                      | 后台子智能体任务管理                                                                   |
| `readSystemPrompt` / `writeSystemPrompt` / `updateSystemPrompt` / `readSystemPromptTemplate`                                           | 系统提示词文件 CRUD                                                                    |
| `readMemory` / `writeMemory`                                                                                                           | 长期记忆文件（`workspace/memory/*`）                                                   |
| `readHeartbeat` / `writeHeartbeat`                                                                                                     | `workspace/HEARTBEAT.md`（始终走 HTTP）                                                |
| `listCronJobs` / `addCronJob` / `updateCronJob` / `runCronJob` / `enableCronJob` / `deleteCronJob`                                     | 定时任务管理                                                                           |
| `listSkills` / `readSkill` / `uploadSkill` / `setSkillActive` / `deleteSkill` / `pinSkill`                                             | 技能管理                                                                               |
| `listChannels` / `updateChannel` / `getChannelConfig` / `updateChannelConfig`                                                          | 通道设置                                                                               |
| `runCuratorReview` / `getCuratorSettings` / `setCuratorSettings`                                                                       | 自动技能 curator 控制                                                                  |
| `listLogFiles` / `readLogFile` / `openLogStream`                                                                                       | 后端日志读取 + 实时 `/logs/ws` 流                                                      |
| `readEnvConfig` / `writeEnvConfig` / `readModelsConfig` / `writeModelsConfig` / `readNetworkConfig` / `writeNetworkConfig`（`env.ts`） | 后端配置文件读取/更新（`GET/PUT /env`、`/models-config`、`/network-config`）           |
| `checkHealth()`                                                                                                                        | 后端可达性（`system_health` IPC 或 `GET /system_prompt`）                              |

浏览器模式聊天流细节：

- 连接 `ws(s)://{VITE_API_BACK_URL}/sessions/agent/ws` 并发送 `{ session_id, msg_id, origin: "user", multi_modal_message }`（`origin` 标记该帧为真实用户输入；服务端会校验并以入口为准打标）
- Base64 媒体先经 `POST /images/upload`、`/audio/upload`、`/video/upload` 上传并以 URL 引用
- 服务端帧：`{ event: "chunk" | "done" | "error" | "stopped" | "hitl_request", ... }`；chunk 携带 `type`（`text`/`reasoning`/`tool_start`/`tool_end`/`tool_result`）与工具元数据
- 流中断后按指数退避重连（1s/2s/4s，经 `WS_RECONNECT_MAX_ATTEMPTS` 最多 3 次）；流中丢失会抛出 `StreamInterruptedError`，并经 mitt 发出 `ws:conn-loss` / `stream:reconnecting` / `stream:reconnected` / `stream:reconnect:failed`
- HITL 中断携带 `HitlInterruptData`（工具名/参数/可选决定）；决定以 `hitl_response` 帧发送（`approve` / `approve_dir` / `yolo` / `reject` / `edit`）。同一张卡片也渲染 Plan-Then-Execute 审批（`exit_plan_mode`）：计划正文、反馈输入、**退回修改**（reject + 反馈）与 **批准** —— 详见 `../wiki/plan-mode/`
- 本页从未发起过的轮次（计划续写的 auto-turn、子代理完成注入、cron 运行）**不带 `turn_started`**——auto-turn 不携带轮次身份——因此它的 chunk 无法流进本地行；`done` 处理器会重取最新一页历史，让最终回答无需刷新即可显示

### WebSocket 单例（ws.ts）

两个相互独立的模块级单例连接（均在 5 秒后自动重连）：

| 连接         | 端点                              | 经 mitt 派发的事件                                                                                                                              |
| ------------ | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 会话推送     | `/sessions/ws?session_id=default` | `ws:connected`、`ws:notification`、`ws:message`、`ws:disconnected`                                                                              |
| 子智能体推送 | `/subagents/ws`                   | `ws:subagents:connected`、`ws:subagents:ready`、`ws:subagent_spawned`、`ws:subagent_ended`、`ws:subagents:message`、`ws:subagents:disconnected` |

两者均从 `VITE_API_BACK_URL` 解析基础 URL（`http://` → `ws://`，`https://` → `wss://`）。

### 客户端使用的后端端点

REST（基础 URL `VITE_API_BACK_URL`，默认 `http://localhost:8080`）：

| 端点                                                                                             | 方法                     | 用途                               |
| ------------------------------------------------------------------------------------------------ | ------------------------ | ---------------------------------- |
| `/sessions`                                                                                      | DELETE                   | 清空会话                           |
| `/n_turns_history_messages`                                                                      | GET                      | 最近 N 轮历史                      |
| `/get_history_by_turn_page`                                                                      | GET                      | 分页历史（经 Dexie 缓存优先）      |
| `/sessions/agent/ws`                                                                             | WS                       | 聊天流、停止、HITL 恢复            |
| `/sessions/ws`                                                                                   | WS                       | 服务端推送通知                     |
| `/subagents/ws`                                                                                  | WS                       | 子智能体生成/结束推送              |
| `/subagents/runs`                                                                                | GET / DELETE             | 子智能体运行记录（列表/子树/删除） |
| `/subagents/steer`                                                                               | POST                     | 操控/恢复子智能体运行              |
| `/system_prompt`                                                                                 | GET / POST / PATCH / PUT | 系统提示词读取/写入/更新           |
| `/system_prompt/template`                                                                        | GET                      | 人格模板文件                       |
| `/memory`                                                                                        | GET / PUT                | 长期记忆文件                       |
| `/heartbeat`                                                                                     | GET / PUT                | HEARTBEAT.md                       |
| `/cron`、`/cron/trigger`、`/cron/enable`                                                         | GET/POST/PUT/DELETE      | 定时任务 CRUD + 触发               |
| `/skills`、`/skills/{path}`、`/skills/upload`、`/skills/toggle`、`/skills/delete`、`/skills/pin` | GET/POST                 | 技能管理                           |
| `/curator/run`、`/curator/settings`                                                              | POST / GET / PUT         | Curator 审查与设置                 |
| `/channels`、`/channels/{name}`、`/channels/{name}/config`                                       | GET / PUT                | 通道开关与配置                     |
| `/env`、`/models-config`、`/network-config`                                                      | GET / PUT                | 后端配置文件读取/更新              |
| `/model-config`                                                                                  | GET                      | 模型配置与有效性                   |
| `/logs/files`、`/logs`                                                                           | GET                      | 日志文件列表与尾部读取             |
| `/logs/ws`                                                                                       | WS                       | 实时日志流                         |
| `/images/upload`、`/audio/upload`、`/video/upload`                                               | POST                     | Base64 媒体上传 → URL              |
| `/media`                                                                                         | GET                      | 渲染持久化的媒体文件               |

---

## 核心模块细节

### SCSS Mixin 库（`common.scss`）

一个 300+ 行的 SCSS mixin 库，为布局、形状、滚动条与文本溢出提供工具（如 `fullViewWindow`、`flexCenter`、`scrollBar`、`wordEllipsis`），被页面、组件与布局广泛使用。

### 离线缓存（db.ts，Dexie/IndexedDB）

- `CachedMessage` —— 镜像后端消息表行（turn_num、images/audios/videos、tool 字段、token 计数）；历史请求缓存优先，只拉取比缓存最大轮次更新的部分
- `CachedCharacter` —— 按会话缓存的头像/名称快照（base64 data URL 或来自 `public/avatar/` 的 `/avatar/*.jpg`）
- 从 `/subagents/ws` 推送与 REST 拉取缓存的子智能体运行记录
- 聊天背景图片配置（全局 Pinia store `stores/chat-background.ts`）
- `clientLog.ts` 将捕获的浏览器 `console.*` 输出持久化到 `all` / `log` / `error` 分桶结构

### 工具调用耗时（`ChatToolCard.vue`）

每张工具卡都会显示这次调用花了多久，与后端测量的口径完全一致：

- 运行中：1 秒一跳的计时器在走（`formatElapsed`，与工具栏"运行中命令"入口同一套惯例）；
- 完成后：定格为实测值（`formatToolDuration`：`<1s → 850ms`、`<10s → 1.2s`、其余整秒）；
- 未知则留空——本功能之前的历史卡片、或被人工拒绝的调用，**不显示**耗时，而不是显示假的 `0ms`。

数字来自两个永远一致的来源：流式 `tool_result` 帧携带 `duration_ms`，历史行携带 `messages.tool_duration_ms`（中间件的权威测量，帧在有戳记时优先采用）。审批等待与排队时间天然不在窗口内。

### 内联编辑 diff（`ChatToolCard.vue` / `DiffPreview.vue`）

`patch_file` 与 `write_file` 的结果渲染为彩色 unified diff 而非裸 JSON：客户端解析后端的 `difflib` 输出（`app/utils/tool-diff.ts`，纯前缀分类——增行绿、删行红、上下文原样、`@@` hunk 头蓝色，带新旧双列行号与 `+N −M` 头部统计）。`write_file` 自 2026-10-24 起返回带 `diff` / `isNew` 的 JSON（新建文件显示「新建文件」徽标并以写入的 `text` 构建全绿预览）；任何解析不了的形状退回原 `<pre>`——构造上即 fail-open。

聊天与文件查看器的 markdown 经 v-safe-html 管线渲染，其中包含 2026-10-24 的渲染增强：Shiki v4 在流式结束后为代码围栏着色（明暗双主题、复制/换行工具栏、流式零开销）、` ```mermaid ` 围栏渲染为经消毒的 SVG 图（点击放大）、KaTeX 渲染 `$…$`/`$$…$$` 公式、文件查看器对 ≤256 KB 的代码文件高亮——全部经 DOMPurify 白名单与受信任根作用域的内联样式（`security.ts`），绝无裸 HTML 通道。

### 状态与事件

- **Pinia**（`stores/`）：UI 状态（`ui.ts`：侧边栏 / 计划停靠折叠持久化）、后台任务（`subagent.ts`）、会话计划（`todo.ts`）、连通性（`connection.ts`）、聊天背景（`chat-background.ts`）、会话开关（`thinking.ts` / `session-model.ts`）、模型档案（`llm-profiles.ts`）、右侧栏（`right-sidebar.ts`）、上下文计量（`context-usage.ts`）、运行中的命令（`running-commands.ts`）、访问模式（`access-mode.ts`）、登录会话（`auth.ts`）、项目目录（`project-directory.ts`）、文件查看（`file-viewer.ts`）、新建会话预设选择（`new-session.ts`）、通知（`notification.ts`）、TaskFlow 进度（`taskflow.ts`）、联网搜索后端（`web-search.ts`）
- **mitt 事件总线**：WS 事件、流重连事件、会话流中断（`session:abort-stream`）、跨组件通知
- **connection store**（`stores/connection.ts`）：监听 `/sessions/ws` 心跳与浏览器 online/offline 事件；暴露 `isOnline` / `backendStatus` 并驱动 `app.vue` 的全局连接横幅

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

**后端**配置位于项目根目录的三个文件：入库的 `.env`（非敏感应用配置）、`.models`（各模型厂商；首次启动从 `.models.example` 生成）与 `.network`（联网搜索 / 浏览器代理 / LangSmith；从 `.network.example` 生成）——后两者不入库。

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
