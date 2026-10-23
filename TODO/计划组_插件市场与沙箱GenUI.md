# 计划组：插件市场 + 沙箱化插件 UI + Gen-UI（长期路线）

- 建档日期：2026-10-23
- 状态：**长期规划，待评审**。这是 sherry 前端最大的功能缺口（ZCode 有完整市场 + 沙箱 + Gen-UI，sherry 的 MCP tab 是空占位符），需分四个阶段逐步落地，每阶段可独立交付。
- **文档范围**：从 sherry 当前空白的 MCP 占位符出发，规划一条到完整插件生态的路线：Phase 0 MCP 服务配置 → Phase 1 插件市场 → Phase 2 沙箱化插件 UI → Phase 3 Gen-UI。每个阶段给出目标、改动清单、工程量、验收标准。
- **硬约束**：每个阶段独立可交付（不跳阶段也可用）；沙箱隔离是安全红线（插件 UI 不能拿到宿主权限）；现有 Channels 功能不破坏；后端改动 additive。
- 关联代码：
  - sherry 现状（起点）：
    - `client/app/pages/home/components/ExtendPanel.vue:62-145`（Channels tab 已实现 + MCP tab 纯占位）
    - `server/trigger/channels/`（channel 子系统——已实现）
    - `server/service/skill_scan_*.py`（技能扫描后端——最接近的插件发现先例）
    - `server/service/browser_manager.py` + `browser_cdp.py`（CDP 浏览器子系统——沙箱先例）
    - `server/service/agent_config_service.py`（配置持久化层——scope 模型可扩展）
  - ZCode 参考（全链路目标）：
    - 市场：`packages/ui/src/settings/PluginsSection.tsx` + `PluginStorePage.tsx` + `store/pluginManagementStore.ts`
    - 沙箱：`packages/desktop/src/main/pluginSandbox/` + `packages/ui/src/plugin-ui/`
    - Gen-UI：`packages/ui/src/gen-ui/` + `packages/shared/src/gen-ui/contract.ts`
    - 协议：`packages/shared/src/mcp-apps/contract.ts`（MCP Apps extension `io.modelcontextprotocol/ui`）

---

## 0. 背景（已核实）

### 0.1 sherry 现状

sherry 的"扩展"面板（`ExtendPanel.vue`）有两个 tab：
- **Channels tab**（`:66-130`）——**已实现**：加载 channel 列表、渲染卡片、打开 `ChannelSettingsDialog`。后端有完整 REST（`GET/PUT /channels`、`GET/PUT /channels/:name/config`），配置持久到 `plugins/channels/<name>/config.json`
- **MCP tab**（`:132-145`）——**纯占位符**：一个虚线空状态框 + "Nothing here yet" + "No MCP servers configured"。无列表、无配置、无 store 调用

客户端**无 MCP store**（22 个 Pinia store 里没有 mcpStore/pluginStore）。后端**无 MCP 服务配置**（grep `server/` 无 mcp/model context protocol/marketplace 命中）。sherry 已有的 `skill_scan_*.py` 后端是最接近的"插件发现"先例，`browser_cdp.py` 是最接近的"嵌入式浏览器"沙箱先例。

### 0.2 ZCode 全链路

ZCode 的插件系统是一个完整生态：

| 层 | ZCode 实现 | 关键特征 |
|---|---|---|
| **市场** | `PluginStorePage.tsx` + `pluginManagementStore.ts` | 多态 source（GitHub repo / git URL / HTTP URL / 本地路径）；安装/卸载/更新/启用/禁用；workspace/user scope；诊断（error/warning）；从竞品 CLI（Claude Code/Codex/Trae/Windsurf）导入 |
| **沙箱** | Electron `<webview>` + 自定义 `zcode-sandbox://` 协议 + `MessagePort` JSON-RPC | `contextIsolation`/`sandbox`/`nodeIntegration=false`；CSP `default-src 'none'` + 声明域；permission gate（camera/mic/geo 经原生对话框授权）；max 64 沙箱实例；持久 partition |
| **插件 UI 渲染** | `plugin-ui/PluginUiCard.tsx`（inline 卡片）+ `PluginUiSidePane.tsx`（侧栏） | MCP 工具返回 `display.ui` 带 `ui://` HTML 资源 → 沙箱渲染；`window.zcode` alias API（callTool/readResource/sendFollowUpMessage 等） |
| **Gen-UI** | `gen-ui/GenUiCard.tsx` + `TweakPanel.tsx` | LLM 输出 `::visualize{path="..."}` 标记 → 渲染 HTML 卡片；TweakPanel 实时调参（slider/color/toggle/select）→ 预览/原始切换 → submit = 重写 Gen-UI 文件 |

### 0.3 sherry 的技术差异

| 关注点 | ZCode（Electron） | sherry（Tauri/Web） |
|---|---|---|
| 沙箱隔离 | Electron `<webview>` + 自定义协议 + 独立 session partition | **iframe `sandbox` 属性**（Tauri webview 内嵌 iframe）+ `postMessage` 传输；或 Tauri 的 `WebviewWindow` 多窗口隔离 |
| 传输协议 | `MessagePort` + JSON-RPC | `window.postMessage` + JSON-RPC（iframe↔宿主） |
| 别名 API | `window.zcode`（注入 alias.js 到 `<head>`） | iframe 内注入 `window.sherry` alias 脚本 |
| CSP 控制 | Electron session CSP header | iframe `sandbox="allow-scripts"` + HTTP CSP header |
| 持久化 | Electron persistent partition | IndexedDB（iframe 内，同源限制需处理） |

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：每阶段独立可交付 | 不跳阶段也可用——Phase 0 完成后 MCP 配置可用，Phase 1 完成后有市场，不依赖后续 | 每阶段有独立验收 |
| **F2**：沙箱隔离红线 | 插件 UI 不能拿到宿主 DOM / Pinia store / WS 连接 / 文件系统直接访问 | 沙箱内代码无法访问 `window.parent` / `localStorage`（宿主的） |
| **F3**：现有 Channels 不破坏 | ExtendPanel 的 Channels tab 功能不变 | Channels 行为 byte-identical |
| **F4**：后端 additive | 新增 service/route，不改现有 | 现有 API 不变 |
| **F5**：fail-open | 插件加载/市场请求失败不影响主流程 | 插件崩溃不崩主 UI |

---

## Phase 0：MCP 服务配置（基础层）

### 目标

替换 `ExtendPanel.vue` 的空 MCP 占位符，实现 MCP server CRUD + 工具发现 + 会话级启用。

### 改动清单

#### 后端

- [ ] **P0.1** 新建 `server/service/mcp_service.py`：
  - MCP server registry（SQLite 或 JSON）：`{name, command, args, env, enabled, scope}`
  - `list_mcp_servers()` / `add_mcp_server(config)` / `update_mcp_server(name, config)` / `remove_mcp_server(name)` / `toggle_mcp_server(name, enabled)`
  - 参考现有 `skill_scan_*.py` 的扫描模式 + `agent_config_service.py` 的配置持久化
- [ ] **P0.2** MCP server 连接管理：
  - 启动/停止 MCP server 进程（stdio/SSE 两种 transport）
  - 工具发现：连接后枚举 server 提供的 tools → 注册到 agent 工具面
  - 参考 sherry 已有的 `agent/tools/__init__.py::build_main_tools()` 扩展点
- [ ] **P0.3** HTTP routes：`GET /mcp/servers`、`POST /mcp/servers`、`PUT /mcp/servers/:name`、`DELETE /mcp/servers/:name`、`GET /mcp/servers/:name/tools`

#### 前端

- [ ] **P0.4** 新建 `client/app/stores/mcp.ts`（Pinia store）：
  - state: `servers: McpServerConfig[]`, `toolsByServer: Record<name, Tool[]>`, `loading`, `error`
  - actions: `load()`、`add(config)`、`update(name, config)`、`remove(name)`、`toggle(name, enabled)`
- [ ] **P0.5** `ExtendPanel.vue:132-145` MCP tab 改造：
  - 替换占位符为 MCP server 列表（卡片网格，同 Channels tab 模式 `:66-130`）
  - 每张卡片：name + command + enabled 开关 + 工具数量 + 编辑/删除
  - "添加 MCP 服务"按钮 → `McpServerDialog.vue`（name/command/args/env 表单）
  - 连接状态指示（运行中/已停止/错误）
- [ ] **P0.6** 新建 `McpServerDialog.vue`：表单 + 连接测试（"测试连接"按钮调 `GET /mcp/servers/:name/tools`）
- [ ] **P0.7** i18n：四语言增 `mcp.*` 键

### 工程量
4-6 天

### 验收
- [ ] **F3**：Channels tab 不受影响
- [ ] **F4**：后端 additive
- [ ] MCP tab 显示 server 列表 + 增删改 + 启停
- [ ] MCP server 工具出现在 agent 工具面

---

## Phase 1：插件市场（发现 + 安装）

### 目标

建立插件市场：多态 source（git/URL/本地）→ manifest 解析 → 安装/卸载/更新/启用。

### 改动清单

#### 后端

- [ ] **P1.1** 新建 `server/service/plugin_service.py`：
  - **Source 解析**（参考 ZCode 多态 source）：
    - GitHub `owner/repo` → `git clone`
    - git URL → `git clone`
    - HTTP URL → 下载 manifest JSON
    - 本地路径 → 直接读
  - **Manifest 解析**：`marketplace.json`（插件列表）+ 每个 plugin 的 `plugin.json`（name/version/description/componentTypes/tools/skills/hooks/mcp）
  - **安装**：clone/下载到 `plugins/installed/<name>/`，扫描 componentTypes，注册 tools/skills/hooks/mcp
  - **卸载**：删除目录 + 注销注册
  - **更新**：`git pull` / 重新下载 + 原子替换注册
- [ ] **P1.2** 新建 `server/DAO/plugin_store.py`（SQLite）：
  - `marketplaces(id, name, source, install_location, last_updated, plugin_count, is_official)`
  - `installed_plugins(id, name, marketplace, version, enabled, scope, install_path, installed_at, component_types)`
  - 参考现有 `server/DAO/` 模式
- [ ] **P1.3** HTTP routes：
  - `GET /plugins/marketplaces` / `POST /plugins/marketplaces`（add source）/ `DELETE /plugins/marketplaces/:id`
  - `GET /plugins/available`（marketplace 里的插件列表）
  - `POST /plugins/install` / `DELETE /plugins/:id`（uninstall）/ `PUT /plugins/:id/update` / `PUT /plugins/:id/enabled`
  - `GET /plugins/installed`
- [ ] **P1.4** 默认市场种子：sherry 官方 marketplace（CDN JSON manifest）——参考 ZCode 的 `zcode-plugins-official`

#### 前端

- [ ] **P1.5** 新建 `client/app/stores/plugin-management.ts`（Pinia store，移植 ZCode `pluginManagementStore.ts`）：
  - state: `marketplaces[]`, `availablePlugins[]`, `installedPlugins[]`, `activeOperations`, `operationVersion`
  - actions: `load()`、`addMarketplace(source)`、`refreshMarketplace(id?)`、`removeMarketplace(id)`、`install(name, marketplace, scope)`、`uninstall(id)`、`update(id)`、`setEnabled(id, enabled)`
  - 诊断：install/update 失败返回 `diagnostics`（error/warning），不 throw
- [ ] **P1.6** 新建 `client/app/pages/home/components/PluginStorePanel.vue`（右栏新 tab，或 ExtendPanel 增第三 tab）：
  - 视图切换：`store`（可用列表）/ `detail`（插件详情）/ `manage`（已安装管理）
  - `PluginStoreCard.vue`：插件卡片（name/description/version/安装按钮/更新标记）
  - `AddMarketplaceSourceDialog.vue`：source 输入（文本框 + 本地路径选择，Tauri `@tauri-apps/plugin-dialog`）
  - `PluginStoreSourcesDialog.vue`：marketplace 源列表 + 刷新 + 删除
  - scope 选择（workspace/user）——参考 ZCode `PluginScopeMenu.tsx`
  - 诊断展示：error/warning 列表
- [ ] **P1.7** ExtendPanel 增 "插件市场" tab（第三 tab，与 Channels / MCP 并列）
- [ ] **P1.8** i18n：四语言增 `plugins.*` 键

### 工程量
8-12 天

### 验收
- [ ] **F1**：Phase 0 完成后即可用；Phase 1 完成后市场可用
- [ ] **F4**：后端 additive
- [ ] **F5**：市场请求失败不影响主 UI
- [ ] 多态 source 添加（git/URL/本地）
- [ ] 插件安装/卸载/更新/启用/禁用
- [ ] 已安装插件工具/skills/hooks/mcp 注册到 agent

---

## Phase 2：沙箱化插件 UI（隔离渲染）

### 目标

让 MCP 工具/插件能返回自定义 HTML UI，在沙箱 iframe 中隔离渲染，通过 `postMessage` + JSON-RPC 与宿主通信。

### 设计决策：sherry 的沙箱方案

sherry 是 Tauri（桌面）+ Web 两种形态。沙箱方案需兼顾：

| 方案 | 适用 | 隔离强度 | 说明 |
|---|---|---|---|
| **iframe sandbox**（首选） | Web + Tauri | 中 | `<iframe sandbox="allow-scripts">` + `postMessage`；CSP header 限制；同源问题用 `srcdoc` 或 blob URL 解决 |
| Tauri WebviewWindow | 仅 Tauri | 高 | 独立窗口/webview，进程级隔离；但多窗口 UX 重 |

**首选 iframe sandbox**——与 ZCode 的 webview 理念一致（隔离渲染 + postMessage 通信），但用 iframe 而非 Electron webview（Tauri renderer 内嵌 iframe 即可）。

### 改动清单

#### 后端

- [ ] **P2.1** MCP 工具结果扩展 `display.ui` 字段（参考 ZCode MCP Apps extension）：
  - 工具返回 `_meta.display.ui = { resourceUri: "ui://...", mimeType: "text/html;profile=mcp-app", visibility: "inline"|"sidebar", csp?: string }`
  - 后端校验 HTML 大小上限（≤16 MiB）+ CSP 声明
- [ ] **P2.2** CSP 构建（参考 ZCode `csp.ts`）：
  - `default-src 'none'` + per-resource 声明域
  - `frame-ancestors` = sherry origin only
  - `form-action 'none'`、`object-src 'none'`
  - `'unsafe-eval'`/`'wasm-unsafe-eval'` 仅在插件声明时放开

#### 前端

- [ ] **P2.3** 新建 `client/app/composables/use-plugin-sandbox.ts`（沙箱管理器）：
  - `createSandbox(html: string, csp: string, origin: string): SandboxHandle`
  - 创建 `<iframe sandbox="allow-scripts" srcdoc="...">`（srcdoc 注入 HTML + alias 脚本 + CSP meta）
  - `postMessage` JSON-RPC 通道（iframe↔宿主）
  - 生命周期：创建 → handshake（`ui/initialize`，15s 超时）→ 运行 → teardown（`ui/resource-teardown`）
  - 容量上限（max 16 沙箱实例，LRU 淘汰——参考 ZCode max 64）
  - fail-open：handshake 失败/超时 → 退化纯工具结果文本

- [ ] **P2.4** 新建 `client/app/utils/plugin-alias.ts`（`window.sherry` alias 脚本，注入到 iframe `<head>`）：
  - 参考 ZCode `aliasApi.ts` 的 `ZcodeAliasApi`，定义 `window.sherry`：
    - 只读状态：`toolInput`、`toolOutput`、`theme`、`locale`、`displayMode`、`hostCapabilities`
    - 方法：`callTool(name, args)`、`readResource(uri)`、`listResources()`、`sendFollowUpMessage({prompt})`、`openExternal({href})`、`downloadFile(items)`、`notifyIntrinsicHeight()`、`setWidgetState()`
  - 所有方法通过 `postMessage` 转发到宿主，宿主校验后执行
  - **安全**：alias 脚本在 iframe 内运行（sandbox 隔离），无法访问 `window.parent`（sandbox 限制）、无法访问宿主 localStorage/Pinia/WS

- [ ] **P2.5** 宿主侧 `postMessage` handler（在 `use-plugin-sandbox.ts` 内）：
  - 接收 iframe 的 JSON-RPC 请求 → 校验 method 白名单 → 执行（调后端 API / 更新 UI 状态）→ 返回结果
  - permission gate：camera/mic/geo 请求 → PrimeVue confirm dialog（宿主层授权）
  - sampling（模型访问）：限频（每会话 4 次/每 agent 8 次——参考 ZCode），结果不进主对话

- [ ] **P2.6** 新建 `client/app/components/plugin/PluginUiCard.vue`（inline 卡片渲染）：
  - 接收 `display.ui` 数据 → 调 `use-plugin-sandbox.ts` 创建 iframe
  - 高度自适应（`notifyIntrinsicHeight` → 更新容器高度，范围 80-1200px）
  - 失败/isError → 退化 `ChatToolCard` 纯文本

- [ ] **P2.7** 新建 `client/app/components/plugin/PluginUiSidePane.vue`（侧栏全屏渲染）：
  - 右栏 tab 形式（`right-sidebar.ts` 增 `pluginUi` kind）
  - 同样走 `use-plugin-sandbox.ts` 但 `displayMode = fullscreen`

- [ ] **P2.8** `ChatToolCard.vue` 改造：检测 `toolResult` 中的 `display.ui` → 渲染 `PluginUiCard`（否则走现有 `<pre>` / diff 预览）
- [ ] **P2.9** 主题投射：宿主 CSS 变量（`--color-*`）投射到 iframe（`postMessage` 推送 `styles.variables`），插件跟随暗/亮主题

### 工程量
12-16 天

### 验收
- [ ] **F2**：沙箱内代码无法访问宿主 DOM/store/WS/文件系统
- [ ] **F5**：插件加载失败退化纯文本
- [ ] MCP 工具返回 HTML → iframe 沙箱渲染
- [ ] `window.sherry` alias API 可用（callTool/sendFollowUpMessage 等）
- [ ] 主题跟随（暗/亮切换同步到插件）
- [ ] permission gate（camera/mic 请求经宿主确认）
- [ ] inline 卡片 + 侧栏全屏两种渲染模式

---

## Phase 3：Gen-UI（LLM 生成交互式组件）

### 目标

让 LLM 输出 `::visualize{path="..."}` 标记 → 渲染交互式 HTML 卡片 + TweakPanel 实时调参。

### 改动清单

#### 协议

- [ ] **P3.1** 定义 Gen-UI 标记协议（参考 ZCode `packages/shared/src/gen-ui/contract.ts`）：
  - LLM 在回复文本中嵌入 `::visualize{path="dashboard.html" title="仪表盘" mode="wide"}`
  - `path` = 小写连字符 `.html` 文件名（严格校验 `^[a-z0-9]+(?:-[a-z0-9]+)*\.html$`）
  - HTML 上限 5MB
  - 解析器：扫描 assistant text → 提取 `::visualize` 标记 → 分割为 `text` + `ui` + `pending` 段

#### 前端

- [ ] **P3.2** 新建 `client/app/composables/use-gen-ui.ts`（Gen-UI 页面控制器，移植 ZCode `adapters/page.ts`）：
  - `createGenUiPage(path, html, target)` → 创建沙箱（复用 Phase 2 的 `use-plugin-sandbox.ts`）+ `AppBridge` handshake
  - 注册 Gen-UI 方法 handler：
    - `zcode/gen-ui/state`（读写 widgetState，≤16KiB）
    - `zcode/gen-ui/follow-up`（发 follow-up 消息，需用户确认）
    - `zcode/gen-ui/tweak`（读 tweak 定义）
    - `zcode/gen-ui/presentation`（展示模式）
  - 主题投射（复用 Phase 2 的 CSS 变量投射）
  - retained page owner（IntersectionObserver 追踪可见性，离屏 5 分钟 suspend，复用 Phase 2 容量管理）

- [ ] **P3.3** 新建 `client/app/components/gen-ui/GenUiCard.vue`（参考 ZCode `GenUiCard.tsx`）：
  - 解析 assistant text 的 `::visualize` 标记 → `text` 段走 markdown 渲染 + `ui` 段走 `GenUiCard`
  - inline 模式（锚点绑定到对话流）+ expand 模式（Dialog 弹窗，同沙箱原子移动不 remount）
  - copy-image（`page.copyImage()` → 离屏 canvas → 剪贴板）

- [ ] **P3.4** 新建 `client/app/components/gen-ui/TweakPanel.vue`（参考 ZCode `TweakPanel.tsx`）：
  - 渲染控件组：slider（`<input type=range>`）、color（`<input type=color>`）、toggle（checkbox）、select（`<select>`）
  - **实时调参**：change → `page.setTweaks(next)` → 投射到沙箱 hostContext → 插件即时重渲染（无 round-trip）
  - **预览/原始切换**：`page.previewTweaks(!preview)` → 投射空值 = 原始状态
  - **Reset**：`page.setTweaks(original)`
  - **Submit**：`page.submitTweaks(values)` → 组装 JSON → `followUp("Update the Gen UI file <path> with these settings:\n<json>")` → LLM 重写 HTML → 卡片重新渲染

- [ ] **P3.5** `ChatBox.vue` 改造：AI 消息渲染检测 `::visualize` 标记 → 分段渲染（text + GenUiCard）
- [ ] **P3.6** Gen-UI 文件存储：LLM 生成的 HTML 存到 `workspace/.sherry/gen-ui/<path>`（per-project），通过 `GET /gen-ui/:path` 读取
- [ ] **P3.7** widgetState 投射回模型：state 作为 `<untrusted_gen_ui_state>` 块注入下一轮 model context（参考 ZCode `buildGenUiModelContext`），输入时 strip（`stripGenUiModelContext`）

### 工程量
10-14 天

### 验收
- [ ] **F1**：Phase 2 完成后即可用；Phase 3 是 Gen-UI 增强
- [ ] **F2**：Gen-UI 沙箱隔离（复用 Phase 2）
- [ ] LLM 输出 `::visualize` → 渲染交互式卡片
- [ ] TweakPanel 实时调参 + 预览/原始切换 + submit 重写
- [ ] inline + expand 两种模式
- [ ] widgetState 回传模型

---

## 总执行顺序与里程碑

```
Phase 0：MCP 服务配置（4-6 天）
  ├─ 后端 mcp_service + routes
  ├─ 前端 mcp store + ExtendPanel MCP tab
  └─ MCP 工具注册到 agent 工具面
      │
      ↓ （Phase 1 依赖 Phase 0 的 MCP/插件注册基础）
Phase 1：插件市场（8-12 天）
  ├─ 后端 plugin_service + source 解析 + DAO
  ├─ 前端 plugin-management store + PluginStorePanel
  └─ 安装/卸载/更新/启用 + 工具/skills/hooks 注册
      │
      ↓ （Phase 2 依赖 Phase 1 的插件体系）
Phase 2：沙箱化插件 UI（12-16 天）
  ├─ MCP 工具 display.ui 扩展
  ├─ iframe sandbox + postMessage JSON-RPC
  ├─ window.sherry alias API
  ├─ PluginUiCard（inline）+ PluginUiSidePane（侧栏）
  └─ 主题投射 + permission gate
      │
      ↓ （Phase 3 依赖 Phase 2 的沙箱基建）
Phase 3：Gen-UI（10-14 天）
  ├─ ::visualize 标记协议 + 解析器
  ├─ GenUiCard + TweakPanel
  ├─ retained page owner + 离屏 suspend
  └─ widgetState 回传模型

总工程量：34-48 天（约 7-10 周）
```

**关键依赖链**：Phase 0 → Phase 1 → Phase 2 → Phase 3。每阶段独立可交付，但后阶段依赖前阶段的基建。

**里程碑**：
- Phase 0 完成 → sherry 有 MCP 服务配置（对标 ZCode 的 MCP settings）
- Phase 1 完成 → sherry 有插件市场（对标 ZCode 的 PluginStorePage）
- Phase 2 完成 → sherry 有沙箱插件 UI（对标 ZCode 的 plugin-ui + sandbox）
- Phase 3 完成 → sherry 有 Gen-UI（对标 ZCode 的 gen-ui + TweakPanel）

---

## 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| Electron webview 沙箱 | sherry 是 Tauri/Web，用 iframe sandbox 替代 |
| 从竞品 CLI 导入（Claude Code/Codex） | ZCode 有 ExternalAgentImportDialog，sherry 第一版不做跨 agent 导入 |
| 插件付费/订阅 | ZCode 有 CodingPlan 付费集成，sherry 不做商业层 |
| 插件自动更新检查 | 第一版手动更新；自动检查后续 |
| 多人插件协作 | 单人使用，协作需后端实时同步 |
| 插件签名/验证 | 第一版信任 market source；签名验证后续安全增强 |

---

## 关键参考

### sherry 内部（起点/复用）
- `client/app/pages/home/components/ExtendPanel.vue:62-145`（Channels 已实现 + MCP 占位）
- `server/trigger/channels/`（channel 子系统——插件后端先例）
- `server/service/skill_scan_*.py`（技能扫描——插件发现先例）
- `server/service/browser_manager.py` + `browser_cdp.py`（CDP 浏览器——沙箱先例）
- `server/service/agent_config_service.py`（配置持久化——scope 模型可扩展）
- `client/app/stores/right-sidebar.ts:7-35`（tab 注册——PluginUiSidePane）
- `client/app/components/chat/ChatToolCard.vue:47-52`（工具结果渲染——PluginUiCard 插入点）
- `client/app/pages/home/components/ChatBox.vue:177`（AI 消息渲染——GenUiCard 插入点）

### ZCode 参考（全链路）
- 市场：`packages/ui/src/settings/PluginsSection.tsx`、`PluginStorePage.tsx`、`store/pluginManagementStore.ts`、`packages/shared/src/plugin-types.ts`、`plugin-marketplaces.ts`
- 沙箱：`packages/desktop/src/main/pluginSandbox/`（`guestPolicy.ts`、`csp.ts`、`permissions.ts`、`contract.ts`）、`packages/ui/src/plugin-ui/`（`PluginUiCard`、`PluginUiSidePane`、`contract.ts`）
- 协议：`packages/shared/src/mcp-apps/contract.ts`（MCP Apps extension `io.modelcontextprotocol/ui`）
- Gen-UI：`packages/ui/src/gen-ui/`（`GenUiCard.tsx`、`TweakPanel.tsx`、`adapters/page.ts`、`adapters/retainedPage.ts`、`adapters/theme.ts`）、`packages/shared/src/gen-ui/contract.ts`
- Alias API：`packages/ui/src/plugin-ui/aliasApi.ts`（`window.zcode` 定义）

### 关联 TODO
- `计划_前端_PDF与Office预览.md`（正交——文件查看器）
- `计划_前端_代码高亮与图表.md`（正交——渲染管线，Gen-UI 沙箱不复用 v-safe-html）
