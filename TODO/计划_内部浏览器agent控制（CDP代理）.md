# 计划：让 agent 控制内部浏览器（方案 C · 本地 Chromium + CDP 代理）

- 建档日期：2026-10-07
- 状态：**已实施完毕**（P0–P5 全部 ✅；唯一未做的可选项在 0.2）
- 触发问题：希望 agent 能像 ZCode 那样驱动「工具箱·浏览器」这个面板——自己开页、
  读内容、点击输入、必要时截图看一眼；同时人还能在同一个页面上操作，并且能挂开发者工具。
- 参考实现：`/home/honor/Desktop/project/ZCode`（Electron `<webview>` + CDP；详见第 1 节）
- 涉及包：`server/service/`、`server/trigger/ws/`、`server/trigger/http/`、`agent/tools/`、
  `client/app/pages/home/components/BrowserPanel.vue`、`client/app/stores/toolbox.ts`、
  `config/features/`、`runtime/`（hooks/lane）
- 验收口径：见第 5 节每个阶段的「验收」；总口径是第 3 节的六条**能力不变量**。

## 0.1 实施进度与实测结论（2026-10-07）

**P0 实测（本机，Xvfb :99，Chrome for Testing 153）**

| 结论 | 实测 |
|---|---|
| 有头 / 无头都能跑 | 有头 42.8 fps、无头 59.8 fps 的 screencast；首帧 ~40 ms；启动 → 端口就绪 ~540 ms |
| **反节流旗标是必须的** | 缺 `--disable-backgrounding-occluded-windows` 等四旗标时只有 2 帧 / 8 s（`browser_cdp.CHROME_FLAGS` 已钉） |
| **静页面不出首帧** | `Page.startScreencast` 后需一次 DOM 触碰强制合成（`BrowserManager._nudge_paint`，已内建） |
| 一页两操作者成立 | 第二个独立 WS 客户端（面板角色）screencast 期间，浏览器级连接同页 `Runtime.evaluate` / 点击 / 输入全部可用 |
| 多页面共一条 WS | `Target.createTarget` + `attachToTarget(flatten=True)` 两页共存，PNG 尺寸各自正确 |
| 自由尺寸 | `Emulation.setDeviceMetricsOverride(393×852 @2)` → PNG 786×1704 精确一致 |
| **`--no-sandbox` 自动降级** | 本机无 userns（PRoot）：标准旗标启动 FATAL "No usable sandbox!"，`launch_browser` 自动以 `--no-sandbox` 重试一次并记日志（`LaunchedBrowser.no_sandbox`） |
| 截图给模型 | 落盘 + 路径返回；模型经既有 `image_to_text` 技能看图（`python_repl` 不能 import 技能模块，SKILL.md 明说用 terminal 跑） |

**已实施**

- P1 后端：`config/features/infra_side/browser_agent.py`（`BROWSER_AGENT`）、
  `server/service/browser_cdp.py`（传输层 + 启动器）、`server/service/browser_manager.py`
  （进程单例：页面注册 / 每会话默认页 / LRU / refs 服务端存储 / screencast 订阅 / 自由尺寸）、
  `server/trigger/http/browser.py`（`/browser/status|page|navigate|close`，开关关闭全 404）。
  hooks 新键 `BROWSER_MANAGER`（`server/__main__.py` 注册 + atexit 按 PID 收进程）。
  测试：`tests/server/service/test_browser_cdp.py`、`test_browser_manager.py`、
  `tests/server/test_browser_endpoints.py`、配置用例（`test_features_infra_side.py`）。真机
  live 一条链全绿（navigate → snapshot(refs) → click(ref) → type(ref) → screencast 3 帧 →
  自由尺寸 → screenshot → shutdown）。
- P2 主代理工具：`agent/tools/browser/**`（8 个，`browser_evaluate` 跟随
  `allow_evaluate`）、进 `_MAIN_TOOLS_BUILDERS`、目录新组 `browser`、playbook 技能
  `skills/builtin/core/browser/SKILL.md`、四语 `config.agent.toolGroup.browser`。

**两处实现偏差（有意）**

1. **子代理拒用**：不新增 `MAIN_ONLY_TOOLS` 减法——仓库已有 per-tool
   `metadata["scope"]="main_only"` 门槛（`apply_tool_policy` 无条件先剔除，`memory` /
   taskflow / todolist 同款），浏览器工具直接打这个标记即可，零改动钉住 ZCode 边界。
2. **超时归属**：不加 `tools_timeouts` 新键——每个操作的时限由 `BROWSER_AGENT` 的
   `nav_timeout_s` / `op_timeout_s` 在 **CDP 传输层**强制（更贴近失败点，错误信息更准）。
3. 技能 `scope` 用 **`main_only`**（3.4 原写 `all`）：子代理拿不到工具，索引里放 playbook
   只会是噪声；且 `skills/loader.py` 加了"功能关则技能不进索引"的门控
   （`_feature_off_skill_names`）。

**P3 已实施（2026-10-08）**

- 后端：`BrowserManager` 新增 `reload` / `page_history` / `history_step` / `open_devtools` /
  `send_input`（裸鼠标/滚轮/按键/文本，字段白名单 + 数值强制 + 缺失 delta 补 0）；
  `page_for` 的默认解析**优先普通页**（devtools 前端永远不会成为隐式的操作对象）；
  页面事件扇出（`subscribe_events`）供面板跟随用户点击。
- 通道：`server/trigger/ws/browser_ws.py`（`/browser/ws?session_id=…`，两层握手门 + 票证）。
  协议：出 `ready`/`frame`（**可丢帧，最新者胜**）/`page`/`error`/`pong`；入
  `nav`/`reload`/`back`/`forward`/`history`/`viewport`/`input`/`watch`/`devtools`/`ping`。
  无人观看时自动 `stopScreencast`。
- 前端：`composables/browser-channel.ts`（复用 `WsConnection` 的固定延迟重连）、
  `bridge/toolbox.ts` 的 `fetchBrowserStatus`、store 每个实例的 `cdp/connected/frame/
  serverCanBack/serverCanForward/devtools`、面板的 CDP 模式（帧显示、指针/滚轮/键盘转发、
  自由尺寸 → `Emulation.setDeviceMetricsOverride`、重连提示条、devtools 按钮切换）。
  **`/browser/status` 为 enabled:false（默认安装）时面板完全是原来的 iframe 形态**。
- **真机实测抓出并修掉的四处**（脚本：真实 Manager + 真实 Chromium 驱动
  `BrowserWSSession`）：
  1. **用户在页面里点链接时地址栏不更新** → 补 `Page.frameNavigated` 事件扇出
     （合并 120ms，子框架忽略）；
  2. 失败导航会把地址栏写成 `chrome-error://chromewebdata/` → 该 URL 不再覆盖地址栏
     （画面仍显示 Chrome 的错误页，与真浏览器行为一致）；
  3. `mouseWheel` 缺少任一 delta 会被 CDP **直接拒绝** → 管理器补默认 0（面板两端都发，
     但裸输入通道必须容错）；
  4. 冷启动要等满 20s 的"无沙箱探测"超时 → 首试只给 6s（健康启动 <1s 出端口），失败即刻
     带 `--no-sandbox` 重试（本机冷启动 22s → 7s）。
  实测链路：nav → 首帧 16.7KB/1192×627 → 点击后地址栏跟随到
  `http://127.0.0.1:8080/browsed-by-panel` → 滚轮出帧 → devtools 以 p2 打开且帧流可用 →
  切回 p1 → 零 error 帧、干净关闭。
- 测试：后端 +16（WS 会话 16 个用例、manager 新动词 7 个）、前端 +28（通道 4 / store 1 /
  面板 CDP 5 等），客户端四件套（typecheck / 755→760 unit / 322 integration / dpdm）全绿。

**P4 / P5 已实施（2026-10-09）**

- **空闲回收**：`BrowserManager._idle_sweep_loop` 每 `idle_sweep_interval_s`（默认 60s）扫一次，
  关闭超过 `idle_timeout_s`（默认 30 分钟）未被使用的页面——**但绝不回收有面板订阅者
  （framers）的页面**（有人在看不算"没人用"）；`idle_timeout_s=0` 关闭该功能；sweep 任务随
  `shutdown()` 一起收。
- **HITL 口径（定案）**：浏览器动作**不设审批门**——面板就是"人在同一页面上"的窗口，每个动作
  实时可见（ZCode 同款）。`browser_evaluate` 由 `SHERRY_BROWSER_ALLOW_EVALUATE` 单独把关
  （默认关）；实践中触发审批的是 `terminal`/技能那条外部路径，与本功能无关。
- **开关入面板**：「环境配置」新增 `SHERRY_BROWSER` 组（`SERVER_HTTP["env_group_prefixes"]`
  += 该前缀）；`GET /env` 给每组标了 `kind`（`model`/`plain`），面板按 kind 渲染——模型组用
  档案管理器，其余是纯键值卡片，所以浏览器开关是一张干净的卡片而不是"假模型组"。
- **本机默认改无头**：`.env` 加了 `SHERRY_BROWSER_HEADLESS=1`。有 DISPLAY 就有头是本功能的
  默认策略（真桌面用户可接管窗口），但本机的 :99 是 Xvfb（`-fbdir`），有头只会凭空多出
  Chrome 窗口——面板/调试器/截图全部走 CDP，无头没有任何损失。
- **文档**：AGENTS.md 新增「Agent-Controllable Browser」一节（服务/路由/工具/面板/边界）；
  四语 client README 的 `BrowserPanel.vue` 条目补上 CDP 模式；本节记录 HITL 与开关口径。

**唯一未做的可选项**：`browser_evaluate` 的「多步脚本」形态（把 evaluate 从单表达式扩成
一段脚本，仍留在进程内、仍由开关把守）——需要时再评估。开关：
`SHERRY_BROWSER_AGENT_ENABLED=1`（+ 可选 `SHERRY_BROWSER_ALLOW_EVALUATE=1`、`SHERRY_BROWSER_HEADLESS`），
`.env` 里加行后重启后端生效；目前不在「环境配置」面板的可编辑键表里（P5 决定是否加入）。

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
| C4 | 开关默认关：未开启时工具不存在于任何工具表（主 agent 与子代理注入都没有） | `GET /agent/catalog` 不含 `browser_*`；researcher 子代理的工具表也不含 |
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
- **工具面**（agent 侧，7 个，全部 `browser_` 前缀，与 ZCode 的 tab API 对齐；**主代理直持**
  ——用户 2026-10-07 定案，见 3.4）：

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

### 3.1 与 MCP 的关系（讨论结论，2026-10-07）

**agent 侧不用 MCP 调 CDP**，链路就是上面那张图：`agent 工具 → BrowserManager →
CDPClient(websockets) → Chromium`。ZCode 的"入口是 MCP"要拆开看：那是 `js`
（node_repl）这个**进程内 REPL MCP**——`agent.browsers` 是被注入 JS 沙箱的宿主对象，
传输最终落在宿主进程的 unix-socket broker 上，**不是"浏览器 MCP 服务器"**。我们的等价物
是**原生工具 + 进程内 BrowserManager**，三条决定性理由：

1. **会话身份**：页面按会话分配（C2/C5）。原生工具从 `InjectedState("session_id")`
   或 `run_manager.config["configurable"]["session_id"]` 拿会话；MCP 调用是纯
   JSON-RPC，langgraph 的 state / runnable config 都不过去，会话 id 只剩"模型可见的
   字符串参数"一条路——可幻觉，还把路由键泄进提示词。
2. **进程拓扑**：本仓库的 MCP 服务器按现有机制是 **stdio 独立进程**
   （`plugins/mcp_server/config.json` 的 `$sys.executable`，且 `build_main_tools()`
   组装期就 spawn），够不着进程内单例（Chromium 子进程、screencast 订阅、会话→页面表），
   也没法往会话 WS 推帧；为同进程两半再搭一条 HTTP MCP 是纯成本，把浏览器管理搬进
   工具构建期又是错误的生命周期。
3. **既有约定全套挂在原生工具上**：`GET /agent/catalog` 分组（新增 `browser` 组）、
   `BULK_ONLY_GROUPS`、`ToolSelectionMiddleware` 的按会话裁剪 + 执行拒绝、
   `REQUIRED_TOOLS`、`TOOLS_TIMEOUTS` 限时、预设「工具」页签、C4 的"关闭即不注册"。
   MCP 工具只落 `mcp` 兜底组，其余要么没有要么得重写一遍。

**将来 MCP 的位置（非目标，留接口）**：若要让**外部** MCP 客户端（ZCode 自己、别的
agent）驱动这个浏览器，facade 架在 P4 的 `runtime/hooks.py` 缝上、复用同一个
BrowserManager——"一个管理器，两个前端"。v1 不做。

**同时排除的捷径**：现成的 Playwright MCP / chrome-devtools-mcp 挂进
`plugins/mcp_server/config.json` 一行就能给 agent 一个浏览器——但那正是方案 B
（自拉无头浏览器，面板看不到也点不到），与本计划"同一页面、两个操作者、devtools
可用"的目标冲突，故不采用。

### 3.2 交互面：工具管能力，技能管玩法（讨论结论，2026-10-07）

**不用 js REPL。** ZCode 需要 `js` 是因为它的浏览器 API 只以"注入 JS 沙箱的宿主对象"
形式存在；我们的 CDP 是服务端 Python 直接能说的 JSON-over-WS，`CDPClient` 就是那层，
模型不必为每个动作写 JS。唯一的"写 JS"面是 `browser_evaluate`（默认关的危险项、单个
表达式，不是通用 REPL）；通用代码逃生舱本来就有 `python_repl`。

**能力 = 原生工具（第 3 节的 7 个 `browser_*`），玩法 = 内置技能（`browser-use`
SKILL.md，无脚本）** —— 即 `todolist` / `taskflow` 已用的先例：能力在工具里，技能只教
"何时用、什么顺序、坑在哪"。

- 技能正文承载：snapshot→act→verify 循环、ref 生命周期（导航即失效）、优先文本快照
  而非截图（token 经济）、大页面用局部快照或 evaluate、iframe 边界、错误恢复、站点配方。
- 攻略**不塞**工具 docstring：工具描述每轮都在提示词里（进缓存），7 段长篇说明会永久占
  上下文；SKILL.md 只在 `skill_view` 时加载。
- **技能不承担能力**（不写 browser 脚本让 agent 经 terminal 跑）：脚本是子进程，够不着
  BrowserManager 单例，只能走 HTTP 门 + 网关 token——等于开一条绕过
  `ToolSelectionMiddleware` / 超时 / 证据账本 / HITL 的第二通道，C4"关闭即不存在"也随之
  失效（脚本还躺在盘上）。
- 技能入索引**跟随开关**（`BrowserAgentConfig.enabled=false` 时 `/agent/catalog` 与技能
  索引里都没有它），与 C4 同一条规则；开关翻转走既有 `invalidate_session_prompt`。

### 3.3 "纯技能 + 伞状脚本"为什么排除（讨论结论，2026-10-07）

把能力放进技能脚本（`scripts/browser.py`，agent 用 terminal / python_repl 跑）看似省事，
但撞三件事：

1. **沙箱断网（设计性硬约束）**。`terminal` / `python_repl` 的子进程在 OS 沙箱可用时被
   bwrap 包裹，argv 是 `--unshare-all`（**无 `--share-net`**）→ 新网络命名空间，连
   loopback 都不可达；脚本唯一的传输（HTTP 到 127.0.0.1:8080 / 直连 CDP 端口）就此断掉。
   `docs/sandbox/README.md` 自己也把"真实 bwrap 下 loopback 桥是否可达"标为**未验证**。
   本机（PRoot 内核不支持 userns，bwrap 装上但 probe 失败）子进程降级无沙箱 →
   **本地"能跑"纯属环境偶然**；正常 Linux 桌面或 `SANDBOX_POLICY=required` 的部署上直接
   死（required 下连 `sandbox=False` 降级都被拒）。绕法（`--share-net` / unix-socket 桥）
   都是给既有安全边界开洞或加新协议，不值。
2. **会话身份要开新通道**。子进程 env 没有任何会话信息（`scrub_env` 只减不加），模型也
   不知道自己的 session id；得给 terminal / python_repl 注入 `SHERRY_SESSION_ID` 之类
   的新 plumbing。原生工具的 `InjectedState("session_id")` / `_extract_session_id` 现成。
3. **容错降级**。严格 JSON schema → 自由文本命令 / 代码参数：参数错了从 provider 侧拒绝
   变成脚本运行时爆炸。

两个"纯技能更好"的假设也不成立：后台（cron）工具集是 `server/__main__.py` 的 hook
lambda（当前 `[python_repl, read_file, write_file]`），工具形态给后台加能力同样只是一行；
"一次调用跑多步循环"若真需要，也应做**进程内**的 gated 工具（`browser_evaluate` 就是这个
位子，多步版本也留在进程内），而不是沙箱外的脚本。

结论：3.2 的分工不变——**能力 = 原生工具，技能 = 无脚本 playbook**。给用户手敲的 CLI
可以有（真实 shell 不在沙箱内），但 agent 能力不建在它上面。

### 3.4 agent 面定案：主代理专用工具（对齐 ZCode，2026-10-07）

**用户定案：7 个 `browser_*` 是主代理工具**（进 `_MAIN_TOOLS_BUILDERS` + 工具目录新组
`browser`），对齐 ZCode——它的浏览器能力就在主代理手里（`js` MCP），而**子代理被 broker
明确拒绝**。我们照抄这条边界：

- 子代理一律拿不到 `browser_*`：`_build_child_agent` 对 `MAIN_ONLY_TOOLS` 集合做减法
  （`general` 的 `tools: inherit` 也拦得住），理由与 ZCode 相同——浏览是主代理的交互面。
- 主代理的上下文成本由既有机制承接：**C4 默认关**（`BrowserAgentConfig.enabled=false` →
  工具不注册、目录里没有），开了以后按预设「工具」页签按会话裁剪——正好就是本轮一并
  要更新的东西。
- 预设联动（`builtinAgentConfig` 从 `GET /agent/catalog` 派生，不硬编码）：
  - 纯净 = 只有 REQUIRED → 无浏览器 ✓ 自动；
  - 编程助手 = 除 memory 外全量 → 含浏览器（默认关时目录里本就没有）✓ 自动；
  - 情感陪伴 = 除 任务与计划/子代理 组外全量 → 含浏览器 ✓ 自动；
  - 全量 = 无意见（全开）✓ 自动。
  已绑定旧预设的会话工具表是 EXACT，需要在「工具」页签补勾（或在预设面板重新应用）
  ——写进文档。
- 技能侧：`browser` playbook 技能（无脚本，见 3.2）`scope: main_only`（子代理拿不到工具，
  索引里放 playbook 只会是噪声）；预设的技能选择沿用现有派生（编程助手只保 CODING_SKILLS
  → 不含；陪伴/全量含）。

### 3.5 再议归属：浏览器能力放"子代理专用"？（讨论结论，2026-10-08）

现状（功能开启、evaluate 开启）主代理为浏览器付：8 个工具 **1,765 tok** + 技能索引行
**62 tok** ≈ **1.8k**，占主工具预算（17,191 / 45 个）的 10.6%；技能正文 802 tok 按需加载。
为了省这 1.8k 翻转成子代理专用 **不值得**：

- 收益侧：主代理省 ~1.8k（≈ 128K 窗口的 1.4%）＋页面文本不进主 transcript（每次快照
  ~1.5–2k tok、10 步浏览 ~15–20k 进主会话——但这条有更便宜的替代：结果侧驱逐/裁剪本就
  在做，快照自身也已截断到 4k 字符文本 + 200 元素）。
- 代价侧（动的都是这套功能的卖点）：① **面板归属**——页面按会话分配、面板也按会话取页，
  子代理专用会让主会话的面板看不到任何页面（要新写"本会话或其子会话最近页"的路由）；
  ② 每次浏览 = spawn + announce 两跳，"顺手看一眼"退化，中途纠偏要走 `sessions_send`；
  ③ 动作发生在 detached 子会话里，聊天主流的工具卡不可见；④ ZCode 对齐被反转（它是主代理
  持浏览器、broker 拒子代理）。另有第二条 plumbing：`close_session` 目前只挂在**会话删除**
  上（`server/service/messages.py`），子代理专用需补"子会话结束 → 释放其页"，否则挂在
  死子会话上的页面只能靠 `max_pages` 的 LRU 兜底。
- 已核实的两条（供加法路线参考）：子图 invoke 的输入自带 `session_id`
  （`spawn/core.py:779`），`InjectedState` 在子代理里可用 → **加法授权没有身份障碍**，只剩
  "把 `scope=main_only` 门槛放成按角色显式授权"这一处；另一种加法是把 browser builders
  加进后台工具的 lambda（`server/__main__.py`，一行）。
- 若"浏览器调试"指的是**网页调试**（console / network / 异常排查）而非交互式浏览：那是另一
  批**读取型**工具（`Runtime.consoleAPICalled` / `Runtime.exceptionThrown` / `Log.entryAdded`
  / `Network.*` / `Debugger.*`），今天尚未实现；这批工具"读一堆、报摘要"的形状本身最适合
  委派，届时按 **subagent-only** 设计（甚至新角色"调试员"）是顺的——但那是新能力，不是翻转
  现有 8 个操作型工具的归属。
- 真正值当的两条路（**都不翻转**）：**结果侧治理**（若主 transcript 被页面文本撑大）与
  **加法授权**（要做无人值守/长时浏览时，让 researcher 也拿浏览器工具——需把
  `scope=main_only` 门槛放成"按角色显式授权"，或把 browser builders 加进后台工具
  的 lambda 一行；主代理保持直连，面板与直控不受影响）。
- 结论：**维持 3.4 的主代理专用**；有实测证据表明主上下文被浏览器结果撑大时，先做结果侧
  治理，再考虑加法授权。

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

### P2 agent 工具（主代理，见 3.4）

- 新 `agent/tools/browser/**`：`build_browser_tools()`，**注册进 `_MAIN_TOOLS_BUILDERS`**；
  `enabled=false` 时返回 `[]`（C4）。工具经 `runtime/hooks.py` 的新钩子解析
  server 侧的 BrowserManager（`agent/**` 不得 import `server/**`）。
- **子代理拒绝**：`_build_child_agent` 从 `filtered_tools` 里减去 `MAIN_ONLY_TOOLS`
  （新集合，含 `browser_*`；`general` 的 `tools: inherit` 也拦得住），对齐 ZCode。
- 新工具组 `browser` 进 `agent/tools/catalog.py` 的 `TOOL_GROUPS`/`TOOL_ORDER`
  （普通组，按工具粒度开关；`BULK_ONLY_GROUPS` 不加）。
- 工具读会话项目目录只用于「下载/上传的落盘位置」，页面本身与目录无关。
- 每页一个 `page` 句柄（默认当前会话最近使用的页面），多页面时由 `page` 参数指定。
- 工具全部经 `tools_timeouts` 的新键限时（导航 15s、快照 5s、操作 5s、截图 10s）。
- 危险项：`browser_evaluate` 只在 `BrowserAgentConfig["allow_evaluate"]` 为真时注册
  （与 ZCode 的"逃生舱"对齐，但默认关）。
- 新内置技能 `skills/builtin/core/browser/SKILL.md`：**纯 playbook、无脚本**、
  `scope: all`（见 3.2）——snapshot→act→verify 的顺序、ref 生命周期、文本优先于截图、
  错误恢复；开关关闭时技能不进索引。
- **预设联动**：`builtinAgentConfig` 的派生已自动覆盖四个内置预设（见 3.4）；补四语面板
  文案与文档；已绑定旧预设的会话需在「工具」页签补勾（EXACT 语义）。
- *验收*：C1/C4/C6；`GET /agent/catalog` 按组显示 `browser`（开关开通时），预设「工具」
  页签可见/可勾；子代理（含 general）工具表无 `browser_*`；关闭开关后目录里没有。

### P3 前端 CDP 模式（已实施，见 0.1）

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

### P4 与现有架构的接线（已实施，见 0.1/0.2）

- `runtime/hooks.py`：新增 `set_browser_manager()` / `get_browser_page(session_id)`，
  agent 侧不 import `server/**`（import-linter 契约不变）。
- 会话生命周期：会话删除 / 长时间空闲 → 页面回收（`max_pages` 上限 + LRU）。
- 安全：`enabled=False` 时所有端点 404、工具不注册；开启后仍只 bind loopback；
  受既有 gateway auth / CSRF / 票证三层保护。
- HITL 取舍（**待讨论**）：terminal 有审批门，浏览器点击没有——浏览器动作等价于"用户
  自己在网页上点"，ZCode 也不设门。建议：读操作免审批；`browser_evaluate` 默认关；
  其余动作记 `evidence ledger` 一行，便于事后追溯。

### P5 测试与文档（已实施，见 0.1/0.2）

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
