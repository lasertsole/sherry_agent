# 计划：让 agent 控制内部浏览器（方案 C · 本地 Chromium + CDP 代理）

- 建档日期：2026-10-07
- 状态：**计划（未开工）**，方案已选：**C（CDP 代理）**
- 触发问题：希望 agent 能像 ZCode 那样驱动「工具箱·浏览器」这个面板——自己开页、
  读内容、点击输入、必要时截图看一眼；同时人还能在同一个页面上操作，并且能挂开发者工具。
- 参考实现：`/home/honor/Desktop/project/ZCode`（Electron `<webview>` + CDP；详见第 1 节）
- 涉及包：`server/service/`、`server/trigger/ws/`、`server/trigger/http/`、`agent/tools/`、
  `client/app/pages/home/components/BrowserPanel.vue`、`client/app/stores/toolbox.ts`、
  `config/features/`、`runtime/`（hooks/lane）
- 验收口径：见第 5 节每个阶段的「验收」；总口径是第 3 节的六条**能力不变量**。

---

## 0. 为什么是方案 C（对照 A / B）

| 方案 | 做法 | 结论 |
|---|---|---|
| A 客户端执行 | agent 工具经 WS 转给页面，由页面里的 JS 在 iframe 内跑 | **只能读同源页面**（跨域 iframe 无法被脚本读取），截图为近似渲染。等于把能力锁死在 localhost 自己的页面上，不满足「像 ZCode 那样」 |
| B 服务端无头 | 后端 Playwright 驱动，agent 用它、客户端不显示 | 能力够，但人看不到也点不到，和「工具箱浏览器面板」是两套东西 |
| **C CDP 代理** | 后端启动**有头 Chromium**（`--remote-debugging-port`，仅 loopback），agent 与前端**同时**通过 CDP 附着同一页面：前端用 `Page.startScreencast` 收帧 + canvas 显示、`Input.*` 回传输入；agent 用同一套 CDP 读快照/点击/截图 | 与 ZCode 同构：一份页面、两个操作者、devtools 天然可用 |

本机已具备条件（开工前复核）：

- Chromium 二进制：`/opt/google/chrome/chrome` → 实测指向
  `~/.cache/ms-playwright/chromium-1243/chrome-linux-arm64/chrome`（Chrome for Testing 153）。
- `websockets>=15.0.1` 已是项目依赖（pyproject），CDP 的 WebSocket 客户端不需要新依赖。
- 服务端固定单进程（`server/__main__.py`：`app.config.processes = 1 / workers = 1`），
  浏览器管理器可以是进程内单例，不存在跨 worker 登记问题。

---

## 1. ZCode 的做法（调研结论，作为对照基线）

| 层面 | ZCode 实现 | 位置（`/home/honor/Desktop/project/ZCode`） |
|---|---|---|
| agent 入口 | **没有 `browser_*` 工具**；唯一入口是 `js`（node_repl MCP）里注入的 `agent.browsers` API，`tab` 上 `goto/url/title/snapshot/click/type/press/scroll/hover/select/check/drag/screenshot/setViewportSize` + `playwright.domSnapshot()` + `cua.*`/`dom_cua.*` | `apps/zcode-cli/packages/core/src/browser-client/**` |
| 传输 | js 单元 → unix socket broker（每进程 token、拒子代理）→ desktop host → Electron main → 对 `<webview>` guest 发 CDP | `bootstrap/src/app/node-repl-browser-broker.ts`、`desktop/src/host/browserControlMainBridge.ts`、`desktop/src/main/browserView/browserGuestManager.ts` |
| 读内容（默认） | Playwright `incrementalAriaSnapshot(mode:"ai")` 的**纯文本 AI/ARIA 树**（剥 `[ref=]`、拼 iframe；顶层 3s、iframe 0.5s/1s 预算） | `desktop/src/main/browserView/browserPlaywrightDomSnapshot.ts` |
| 读内容（动作） | 结构化 DOM 快照：可交互元素分配 `ref='eN'` 存 `window.__zcodeRefs`，返回 `selector/xpath/rect/role/name/text/value/...`，**上限 200 元素 / 300 节点**，超限 `truncated` | `desktop/src/main/browserView/browserCommandScripts.ts` |
| 逃生舱 | `playwright.evaluate` / `command.evaluate`（JSON 序列化）、`playwright.elementInfo({x,y})` | 同上 + `browserPlaywrightExecutor.ts` |
| 截图 | 始终 base64 PNG；优先合成表面（缩回 CSS px），否则 `Page.captureScreenshot`（4096 边 / 16.7MP 上限）；模型只能经 `emitImage()` 看到 | `browserGuestManager.ts:1738`、`browserScreenshotCapture.ts` |
| 尺寸 | `Emulation.setDeviceMetricsOverride` 驱动模拟设备框；自由尺寸边界 320–3840 × 320–2160、zoom fit/50…200 | `browserGuestManager.ts::setTabViewport`、`shared/src/browser-use/command-metadata.ts` |
| devtools | `webview.openDevTools()`（原生 webview API；**网页端不存在等价物**） | `packages/ui/src/browser-use/UnifiedBrowserView.tsx:912` |

我们已按其中「尺寸 / zoom / 句柄 / 边界」实现完毕（`16e14c47`）；本计划的其余部分是把
**agent 侧**补齐，并把面板从 iframe 换到 CDP 画面。

---

## 2. 目标与不变量

### 2.1 目标

1. agent 能对**任意站点**（不只是 localhost）开页、读内容、操作；
2. 人在同一个标签页里看到的是**同一个实时页面**（不是截图快照），可以接管操作；
3. 面板继续住在右栏「当前会话」，仍可多开（每个实例一个页面）；
4. 该浏览器**可以挂开发者工具**（CDP 的副产品）；
5. 全部能力**默认关闭**，按会话/全局显式开启。

### 2.2 能力不变量（验收用）

| # | 不变量 | 判定方式 |
|---|---|---|
| C1 | 不入库：浏览器内容不写进 MesMemory/检查点（除非 agent 主动写摘要），只经工具结果进上下文 | 抓一次满屏快照后查 `messages` 表内容长度与来源 |
| C2 | 单进程单例：一个 workspace 一个 Chromium 进程，页面按会话分配 | `GET /browser/status` 报告 pid + 页面数；重启后无孤儿进程 |
| C3 | loopback-only：调试端口只 bind 127.0.0.1，且 CDP 端点不出后端进程 | 端口扫描 + `/browser/status` 不回传端口 |
| C4 | 开关默认关：未开启时相关工具不存在于工具表 | `GET /agent/catalog` 不含 `browser_*` |
| C5 | 人在回路可接管：同一页面既能被 agent 操作也能被用户操作，互不锁死 | 自动化操作进行中仍能在面板里手点 |
| C6 | 资源有界：截图/快照有大小与超时上限，页面数有上限 | 超限返回结构化错误而不是卡住 |

---

## 3. 总体设计

```
                 ┌──────────────── server (单进程) ────────────────┐
                 │ BrowserManager (进程单例)                        │
                 │  ├─ chromium 子进程 (--remote-debugging-port=0)  │
                 │  ├─ CDPClient (websockets, JSON-RPC over WS)     │
                 │  └─ PageSession[n] (一个标签页 = 一个页面)        │
                 └───┬───────────────────────────┬─────────────────┘
          HTTP/WS    │                           │  工具调用
   ┌─────────────────┴──────────┐        ┌───────┴─────────────────┐
   │ client BrowserPanel (CDP 模式)│        │ agent tools            │
   │  canvas ← Page.startScreencast│        │ browser_navigate        │
   │  Input.* ← 鼠标/键盘/滚动      │        │ browser_snapshot        │
   │  地址栏 → Page.navigate       │        │ browser_click/type/...  │
   │  自由尺寸 → Emulation.*       │        │ browser_screenshot      │
   └──────────────────────────────┘        └─────────────────────────┘
```

要点：

- **一个页面两个操作者**。CDP 允许并发连接；agent 与前端各自建 WS 连接，互不阻塞（C5）。
  需要互斥的只有「谁在导航」这类会互相打断的动作——用页面级 `asyncio.Lock` 只锁
  navigate/load 等待，不锁读。
- **前端不再用 iframe**：`BrowserPanel.vue` 增加 CDP 模式（默认即 CDP，`iframe` 保留为
  未启用浏览器服务时的降级路径，见 P3）。画面用 `Page.startScreencast` 的 JPEG 帧画在
  `<canvas>` 上；输入事件转发 `Input.dispatchMouseEvent` / `dispatchKeyEvent` /
  `dispatchMouseEvent(type=mouseWheel)`；地址栏、前进/后退/刷新改走 `Page.navigate` /
  `Page.getNavigationHistory` / `Page.reload`；**自由尺寸改走
  `Emulation.setDeviceMetricsOverride`**（与 ZCode 完全一致，我们现有 store 的
  `viewport/zoom` 原样复用）。
- **工具面**（agent 侧，7 个，全部 `browser_` 前缀，与 ZCode 的 tab API 对齐）：

| 工具 | 参数 | 返回 |
|---|---|---|
| `browser_navigate` | `url`, `page`(可选) | 最终 URL / 标题 / 状态 |
| `browser_snapshot` | `page`, `max_elements=200`, `include_hidden=false` | AI/ARIA 文本树（首选）；`ref`、截断标记 |
| `browser_click` | `ref` 或 `x,y`, `button`, `double` | 操作后的一小段快照（可选） |
| `browser_type` | `text`, `ref?`（缺省打到已聚焦元素）, `submit?` | 同上 |
| `browser_press` / `browser_scroll` | `keys` / `delta_y` | 同上 |
| `browser_screenshot` | `full_page?`, `ref?` | PNG（见 P0-3 的给模型方式） |
| `browser_evaluate` | `expression`（仅开启「危险操作」开关时暴露） | JSON 值 |

- **读内容的三层**（照抄 ZCode 的取舍）：默认给模型看**文本**（ARIA 树 / 结构化 DOM），
  需要视觉时才截图；`evaluate` 是显式危险项，单独开关。

---

## 4. 分阶段

### P0 研究性 spike（1 天，先做，结论决定后面怎么写）

- **P0-1 启动 + 附着**：脚本启动
  `chrome --headless=new? --remote-debugging-port=0 --user-data-dir=<tmp> about:blank`，
  从 stderr/`DevToolsActivePort` 拿端口，用 `websockets` 连 `Page`/`Target` endpoint，
  跑通 `Target.getTargets`、`Page.navigate`、`Page.captureScreenshot`。
  *验收*：一条命令打印页面标题与截图字节数；**有头**模式在无 X 的环境里的可行性同时确认
  （可能需要 `Xvfb` 或 `--headless=new` + screencast，二者取舍写进本文件）。
- **P0-2 screencast**：`Page.startScreencast(format=jpeg, quality≈70, maxWidth/maxHeight)` →
  `Page.screencastFrame` 事件 → `Page.screencastFrameAck`；测一帧大小与 CPU。
  *验收*：本地页滚动时帧率与负载可接受（记录实测值）。
- **P0-3 截图给模型**：确认我们的链路能不能在 `ToolMessage` 里带图片
  （langchain 的 content block + 我们 `message_persistence` 的落库形态），
  或者退化为「截图写临时文件 + 让 agent 用 `read_file`/多模态链看一眼」。
  *验收*：一次真实调用的模型确实"看到"了图（把图里的随机串说出来）。
- **P0-4 输入转发**：`Input.dispatchMouseEvent` / `dispatchKeyEvent` 用 CDP 复现一次点击与输入。

### P1 浏览器服务（后端核心）

- 新 `config/features/infra_side/browser_agent.py`（`BrowserAgentConfig`）：
  `enabled: bool`（默认 False）、`executable: str`、`headless: bool`、`max_pages: int`、
  `screencast_quality/max_width`、`nav_timeout_s`、`snapshot_max_elements`、
  `screenshot_max_bytes`、`user_data_dir: str`（默认 `<ROOT>/src/data/browser-profile`）。
- 新 `server/service/browser_manager.py`：进程单例 `BrowserManager`（懒启动 Chromium、
  CDP 连接池、`PageSession` 分配/回收、进程退出与 `atexit` 清场、崩溃自愈一次）。
- 新 `server/service/browser_cdp.py`：极薄的 CDP 客户端（`websockets`，JSON-RPC 自增 id，
  事件订阅、超时、断线重连）。
- `server/trigger/http/browser.py`：`GET /browser/status`、`POST /browser/page`
  （开/关一个页面 → 返回 page id）、`POST /browser/navigate`。
- *验收*：C2/C3/C6；`/browser/status` 如实报告，关闭后无孤儿进程。

### P2 agent 工具

- 新 `agent/tools/browser/**`：`build_browser_tools()`，注册进 `_MAIN_TOOLS_BUILDERS`；
  工具读会话项目目录只用于「下载/上传的落盘位置」，页面本身与目录无关。
- 每页一个 `page` 句柄（默认当前会话最近使用的页面），多页面时由 `page` 参数指定。
- 工具全部经 `tools_timeouts` 的新键限时（导航 15s、快照 5s、操作 5s、截图 10s）。
- 危险项：`browser_evaluate` 只在 `BrowserAgentConfig["allow_evaluate"]` 为真时注册
  （与 ZCode 的"逃生舱"对齐，但默认关）。
- *验收*：C1/C4/C6；工具在 `GET /agent/catalog` 中按组显示（新组 `browser`），
  预设角色的「工具」页签能整体开关（`BULK_ONLY_GROUPS` 新成员，与 tasks/subagents 同规则）。

### P3 前端 CDP 模式

- `BrowserPanel.vue`：新增 `mode: 'cdp' | 'iframe'`（服务未启用 → 自动落回 iframe）。
  - `canvas` 显示 screencast 帧（`<img>` + base64 也行，二选一，看 P0-2 的帧率）；
  - 地址栏 / 前进后退 / 刷新改走 HTTP 包装（后端转发 CDP，前端不直连 CDP 端口）；
  - 输入转发：指针（含滚轮）与键盘，坐标映射要处理 canvas 缩放（我们在自由尺寸里已有
    `frameScale`，复用同一套换算）；
  - 自由尺寸 / zoom → `Emulation.setDeviceMetricsOverride`（现有 store 的
    `viewport/zoom` 不改语义）；
  - 调试工具按钮：CDP 模式下改为真正的「打开开发者工具」——
    后端 `Target.openDevTools` 不可用于 remote 客户端，因此实现为**开启一个
    `devtools://` 页面并把它的 screencast 也推给前端**，或退化为「在新窗口打开该页」。
    P0 里确认走哪条。
- 新增 WS 通道：`/browser/ws`（`?ticket=` 走既有 WS 票证；JSON 帧：`frame` / `input` /
  `nav` / `event`），与现有 `push_channel` 同层但独立，避免污染会话消息流。
- *验收*：C5；人在面板里点击与 agent 操作互不冲突；断线重连后画面恢复。

### P4 与现有架构的接线

- `runtime/hooks.py`：新增 `set_browser_manager()` / `get_browser_page(session_id)`，
  agent 侧不 import `server/**`（import-linter 契约不变）。
- 会话生命周期：会话删除 / 长时间空闲 → 页面回收（`max_pages` 上限 + LRU）。
- 安全：`enabled=False` 时所有端点 404、工具不注册；开启后仍只 bind loopback；
  受既有 gateway auth / CSRF / 票证三层保护。
- HITL 取舍（**待讨论**）：terminal 有审批门，浏览器点击没有——浏览器动作等价于"用户
  自己在网页上点"，ZCode 也不设门。建议：读操作免审批；`browser_evaluate` 默认关；
  其余动作记 `evidence ledger` 一行，便于事后追溯。

### P5 测试与文档

- 单测：CDP 客户端（假 WS 服务器回放 `Target.getTargets` 等）、`PageSession` 生命周期、
  快照序列化（上限与截断）、工具参数校验。
- 集成（`tests/full/`，标 `llm_e2e`）：真实 Chromium 起一个本地页 → navigate → snapshot →
  click → screenshot，一条链路跑通。
- 前端：screencast 帧渲染、输入事件的坐标换算、自由尺寸映射到 `Emulation.*` 的参数断言。
- 文档：AGENTS.md（新服务/工具/WS）、四语 client README（面板 CDP 模式）、
  `docs/context-governance/`（浏览器内容不进上下文的那条不变量）、本文件手写"完成"。

---

## 5. 风险与未决问题（讨论清单）

1. **有头 vs 无头**：无 X 的环境里"有头"需要 `Xvfb`（多一个系统依赖）；纯
   `--headless=new` 下 screencast 可用，但用户无法真正"接管"原生窗口——只是接管我们的
   canvas。倾向前者（Xvfb + 有头），P0 实测后定。
2. **截图给模型**：P0-3 的结论决定 `browser_screenshot` 是"直接给图"还是"落盘 + 让 agent
   自己 read"。后者更省 token 也更可控，但多一步。
3. **devtools 的形态**：CDP 远程不能开 guest 的原生 devtools 窗口；候选是
   （a）把 `devtools://devtools/bundled/inspector.html?ws=...` 当普通页面打开并 screencast，
   （b）在新窗口打开页面让用户用宿主浏览器调试。P0 里试 (a)。
4. **下载/上传**：CDP 的 `Browser.setDownloadBehavior`、`DOM.setFileInputFiles` 先不做，
   留接口位；`input[type=file]` 在 UI 里不可用要如实提示。
5. **代理与自签证书**：公司代理/自签 HTTPS 需要 `--proxy-server` 与
   `--ignore-certificate-errors` 之类的开关，把口径写进 config（默认跟随系统代理）。
6. **资源**：Chromium 常驻约 150–300MB RSS；`max_pages` 与空闲回收策略要实测后定值。
7. **合规**：agent 读你正在浏览的网页内容属于隐私边界，默认关 + 会话级开关 + 面板上
   常驻一个"agent 正在读取此页面"的提示（P3 里加）。
