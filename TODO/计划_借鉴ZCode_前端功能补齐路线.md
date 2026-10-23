# 计划：借鉴 ZCode 前端优秀功能补齐 sherry（分档路线图）

- 建档日期：2026-10-23
- 状态：**待评审**。本 TODO 是前端功能补齐的总体路线图，由 `D:\selfProj\ZCode-main` 前端盘点与 sherry `client/` 前端盘点交叉对比得出；各功能可独立拆子 TODO 推进。
- **文档范围**：识别 ZCode 前端相比 sherry 的功能差距，按 ROI（用户价值 × 与 sherry 架构契合度）分三档，给出每项的借鉴方案、改动对象与工程量估计，并排定落地顺序。**主旨是"抄交互范式、重写实现"——两边技术栈不同（sherry = Vue3/Nuxt4/PrimeVue/Tailwind；ZCode = React/shadcn/Lexical/streamdown），不涉及代码移植。**
- **硬约束**：不破坏现有 WS message 协议与 message store；新功能一律 additive（新 tab/新组件/新可选 frame），现有渲染 byte-identical；所有渲染增强走 sherry 已有的 `v-safe-html` 安全管线的扩展，不另开未审计的 HTML 注入通道。详见 §1
- 关联代码：
  - sherry 已有基础设施（可复用）：
    - `server/service/trajectory_store.py`（`trajectory_events` 表 + `turn_id` 索引 + `GET /sessions/:session_id/trajectory`——**轨迹调试视图的后端已就绪**）
    - `client/app/constants/security.ts`（DOMPurify allowlist——当前刻意未开 shiki/math/mermaid）
    - `client/app/composables/use-chat-turn-fold.ts`（turn 折叠已有派生分类）
    - `client/app/composables/mitt.ts`（app-wide 事件总线，命令面板/快捷键可复用）
    - `client/app/pages/home/components/RightSidebar.vue`（右栏 tab 宿主 + 面板注册表 `dialogs.ts`）
  - ZCode 参考（对齐目标）：
    - `ZCode-main/.../packages/ui/src/components/ai-elements/code-block.tsx`（Shiki 高亮 + 工具栏）
    - `ZCode-main/.../packages/ui/src/ModelTrajectoryPane.tsx` + `ModelTrajectoryTimeline.tsx`（轨迹调试视图）
    - `ZCode-main/.../packages/ui/src/command-center/CommandCenterDialog.tsx`（命令面板）
    - `ZCode-main/.../packages/ui/src/v4/conversationTimelineLiveTail.ts` + `ConversationRowView.tsx`（消息编辑/重新生成/分支）
  - 关联 TODO：与 `计划_借鉴ZCode_turn折叠展示.md`（已实现）正交，本 TODO 不再涉及折叠

---

## 0. 背景：对比结论（已核实）

### 0.1 两边都是工程级产品，侧重点不同

| 维度 | sherry 更强 | ZCode 更强 |
|---|---|---|
| 会话展示 | turn 折叠 + 转盘跳转（`useChatTurnFold` + `ChatTurnScrubber`） | 消息编辑/重生成/分支（checkpoint + fork） |
| 浏览器 | CDP 8 向缩放手柄 + 移动视口模拟 | Playwright 自动化 + CUA 集成 |
| 上下文 | 用量环 + 压缩阈值刻度 + 缓存命中率 + Context Viewer | context-usage meter（较朴素） |
| 配置 | Persona 预设体系（纯净/编程/情感/全量 + 工具/中间件/技能/模型绑定） | subagent/agent 配置分散 |
| 技能 | 策展器（自动合并/去重/归档） | 无治理层 |
| 进度 | 统一进度浮窗（todos + TaskFlow 合并去重） | TodoPanel + BackgroundWorks 分开 |
| 代码渲染 | **无高亮、无 mermaid、无公式** | Shiki + Mermaid + KaTeX + 工具栏 |
| 命令/快捷键 | 无命令面板、无快捷键系统 | CommandCenter + 可重绑快捷键面板 |
| 会话内搜索 | 仅侧边栏标题/日期过滤 | 会话内全文搜索 + 高亮跳转 |
| 插件 | MCP 占位符 | 完整市场 + 沙箱 UI + Gen-UI |
| 分享 | 无 | 只读时间线链接 + 独立 web-share 渲染 |
| 引导 | 无 onboarding | 欢迎流程 + 模式选择 |

### 0.2 差距清单（按是否已有后端支撑分类）

**后端已就绪、纯前端工作（低成本）：**
- 轨迹调试视图——sherry 后端 `trajectory_store.py` + `GET /sessions/:id/trajectory` 已建好，前端无查看器
- 语音输入——sherry 后端 STT 已是可配模型族，输入框未集成录音按钮

**后端需小改、前端中等工作量：**
- 代码高亮 + Mermaid + KaTeX——纯前端渲染管线扩展，不碰后端
- 命令面板 + 会话内搜索——前端基建，后端零改
- 消息反馈 👍👎——前端 + 一个轻量遥测上报接口
- PDF/Office 预览——前端渲染器接入（pdf.js / docx 解析）
- 快捷键系统——前端统一层 + 设置面板

**后端需中/大改：**
- 消息编辑/重生成——需后端支持 editUserQuery + checkpoint 回退
- 会话分支 Fork——依赖后端 checkpoint 能力
- 会话分享——需后端分享链路
- 插件市场——全链路，长期方向

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：现有 WS message 协议向后兼容 | 现有 `messages` / `turn_started` / `done` 等 frame 不动；新增 frame 一律追加 | 老前端版本访问新后端不崩 |
| **F2**：现有 message store 不动 | `MesMemory` / `messages` 表 schema 不变 | 现有 message 读写 byte-identical |
| **F3**：渲染增强走安全管线扩展 | shiki/math/mermaid 输出必须经过 `v-safe-html` 扩展后的 allowlist 或独立沙箱（iframe），不另开裸 HTML 注入 | 无未经 DOMPurify 的 HTML 进 DOM |
| **F4**：新功能 additive | 新 tab/新组件/新可选 frame；现有渲染（user bubble / assistant text / tool card）保留 | 现有渲染 byte-identical |
| **F5**：fail-open | 任何增强在数据缺失/组件加载失败时退化到现有线性展示，不崩 | 异常情况不崩 |
| **F6**：不强制 LLM 配合 | 不依赖 LLM 改输出格式（消息编辑/反馈等是前端 + 协议层做的） | 增强独立可用 |

---

## 2. 第一档：高价值、契合度高、建议优先

### 2.1 代码块语法高亮 + Mermaid 图 + KaTeX 公式

**差距**：sherry 的 `security.ts` 用 DOMPurify allowlist 一刀切掉了 shiki/hljs/math/mermaid/task-list，代码型 agent 对话里代码块全是黑白纯文本，无高亮、无流程图、无公式。这是 sherry 前端最显眼的体验短板。

**ZCode 做法**：用 `streamdown` + `@streamdown/code`(Shiki) + `@streamdown/mermaid` + `@streamdown/math`，代码块带"放大/换行/复制/语言标签"工具栏（`code-block.tsx`），Mermaid 有 pan/zoom 预览弹窗（`diagram-preview-dialog.tsx`）。

**借鉴方案**：
- 在 sherry 的 markdown-it 管线后接 Shiki（Shiki 输出是带 class 的纯 HTML，不执行 JS，安全可控），DOMPurify allowlist 增补 Shiki 的 span class 白名单
- Mermaid 走沙箱 iframe 渲染（`sandbox="allow-scripts"` 但同源策略隔离，或预校验 diagram 文本后服务端渲染）
- KaTeX 走 `katex.renderToString`（纯字符串输出，不经 eval）
- 代码块加工具栏：复制（已有 `ChatCopyButton`，可复用）、语言标签、行号

**改动清单**：
- [ ] **2.1.1** `client/app/constants/security.ts`：DOMPurify allowlist 增补 Shiki span class + KaTeX span class（白名单枚举，非通配）
- [ ] **2.1.2** 新建 `client/app/composables/use-shiki.ts`：懒加载 Shiki highlighter（WASM，按需初始化），按主题切换
- [ ] **2.1.3** `client/app/components/chat/` 新建 `CodeBlockEnhanced.vue`：替换当前纯 `<pre>` 渲染，集成 Shiki + 工具栏
- [ ] **2.1.4** 新建 `MermaidBlock.vue`：沙箱 iframe 渲染 mermaid 图，带 pan/zoom
- [ ] **2.1.5** KaTeX 集成到 markdown-it（`markdown-it-katex` 或自写插件，`renderToString`）
- [ ] **2.1.6** 流式 tail 渲染兼容（`bubbleView()` 流式截断时不高亮，settle 后高亮）

**工程量**：3-4 天
**验收**：F3 + F4 + F5。新建 `client/app/components/chat/__tests__/CodeBlockEnhanced.test.ts`（高亮输出经 DOMPurify、安全 allowlist 覆盖、fail-open 退化纯文本）。

---

### 2.2 轨迹调试视图（Model Trajectory Pane）

**差距**：sherry 后端已有 `trajectory_events` SQLite 表 + `GET /sessions/:session_id/trajectory`（按 turn 返回 tool_start/tool_end/turn_start/turn_end 事件），**但前端没有查看器**。这是最低成本的一项。

**ZCode 做法**：`ModelTrajectoryPane.tsx` + `ModelTrajectoryTimeline.tsx` + `ModelTrajectorySearchBar.tsx`——原始协议/token/usage 检查器，支持搜索和全部展开/折叠。

**借鉴方案**：
- 纯前端：新建右栏 tab `TrajectoryPanel.vue`，调现有 `GET /sessions/:id/trajectory`
- 渲染：时间线（每个 tool call 一行，start/end 合并显示时长 + args + result），turn 级聚合（总时长 + TTFB）
- 搜索栏：过滤 tool name / args 文本
- 可选：全部展开/折叠

**改动清单**：
- [ ] **2.2.1** `client/app/pages/home/components/RightSidebar.vue` + `dialogs.ts`：注册新 tab `trajectory`
- [ ] **2.2.2** 新建 `client/app/pages/home/components/TrajectoryPanel.vue`：调 `GET /sessions/:id/trajectory?turn_id=...`，时间线渲染
- [ ] **2.2.3** 新建 `client/app/composables/use-trajectory.ts`：封装 API 调用 + 按 turn 分组
- [ ] **2.2.4** 搜索栏 + 展开/折叠控制
- [ ] **2.2.5** 工具卡 `ChatToolCard.vue` 加"查看轨迹"跳转（点击 token 数已有跳 Context Viewer 的先例）

**工程量**：2 天
**验收**：F4 + F5。后端 API 不动（零后端改动）。新建 `client/app/pages/home/components/__tests__/TrajectoryPanel.test.ts`。

---

### 2.3 消息编辑 + 重新生成

**差距**：sherry 是 append-only 模型，明确没有消息编辑/重生成，靠折叠代替分支。编辑历史用户消息、重新生成 AI 回答是编码 agent 的基础操作。

**ZCode 做法**：`SingleUserInputRowView` 点击编辑展开完整编辑器（带附件/上下文 chip），提交 `editUserQuery`；进阶版有**编辑即回退工作区文件**（`ConversationFileRewindDialog.tsx`，带冲突预览 + "仅会话回退"兜底）。

**借鉴方案（分两阶段）**：
- **阶段一（基础）**：编辑用户消息 → 截断该消息之后的全部 messages → 重新提交。需要后端支持 `editUserQuery`（截断 message store 到指定 turn + 重跑）
- **阶段二（工作区回退）**：编辑时可选"回退工作区文件到该 turn"，用 shadow-git（sherry 已有 `shadow-git/` per-project write snapshots）做快照回退，带冲突预览弹窗

**改动清单（阶段一）**：
- [ ] **2.3.1** 后端：新 API `POST /sessions/:id/edit`，body `{turn_id, new_text}`——截断 message store 到该 turn_id（不含）+ 用 new_text 重跑
- [ ] **2.3.2** 前端：`ChatBox.vue` 用户消息行加编辑按钮（hover 显示），点击展开 `inputBox` 编辑器（复用现有 composer + chip 体系）
- [ ] **2.3.3** 前端：编辑提交后调 `POST /sessions/:id/edit`，前端截断本地 messages 到该 turn + 重跑流式
- [ ] **2.3.4** 约束：编辑仅限用户消息；编辑后该 turn 之后的所有 AI/工具消息标记为 `superseded`（保留可恢复，不物理删除）

**改动清单（阶段二，可选）**：
- [ ] **2.3.5** 后端：`POST /sessions/:id/edit` 增 `rewind_files: bool`——用 `workspace/.sherry/shadow-git/` 快照回退到该 turn 的文件状态
- [ ] **2.3.6** 前端：`FileRewindDialog.vue`——冲突预览（difflib 对比当前文件 vs 快照），"仅会话回退"兜底按钮

**工程量**：阶段一 4-5 天；阶段二 3-4 天（依赖 shadow-git）
**验收**：F1 + F2（message store 截断是逻辑删除，schema 不变）+ F6。新建 `tests/server/test_edit_query.py` + 前端集成测试。

---

### 2.4 命令面板 / Quick Pick（Cmd+K）

**差距**：sherry 没有命令面板。功能散落在九宫格菜单和各面板里。

**ZCode 做法**：`CommandCenterDialog.tsx` + `TaskFindDialog.tsx`——跨工作区任务搜索 + 命令触发统一入口，附带搜索历史。

**借鉴方案**：
- 全局 `Cmd+K`（macOS）/ `Ctrl+K`（Win/Linux）唤起命令面板
- 命令来源：会话操作（新建/切换/删除）、设置入口（打开各右栏 tab）、工具栏操作（切换模型/模式/思考）、技能/slash 命令
- 搜索历史 + 模糊匹配
- 复用 sherry 已有的 `mitt.ts` 事件总线做命令分发

**改动清单**：
- [ ] **2.4.1** 新建 `client/app/components/CommandPalette.vue`：PrimeVue `Dialog` + `AutoComplete`/自写模糊匹配
- [ ] **2.4.2** 新建 `client/app/composables/use-command-registry.ts`：命令注册表（name + 快捷键 + handler + 图标）
- [ ] **2.4.3** `app/pages/home/index.vue`：挂载 `CommandPalette` + 注册全局 `Ctrl+K` 快捷键
- [ ] **2.4.4** 命令来源接入：会话操作（sidebar 已有 mitt 事件）、设置 tab（RightSidebar 已有注册表）、工具栏操作
- [ ] **2.4.5** 搜索历史持久化（Dexie，复用 `useDraftPersistence` 的 IndexedDB 模式）

**工程量**：3-4 天
**验收**：F4。新建 `client/app/components/__tests__/CommandPalette.test.ts`。

---

### 2.5 会话内搜索（Find in Conversation）

**差距**：sherry 侧边栏有标题/日期过滤，但**会话内无全文搜索**。会话折叠后内容多，缺 Ctrl+F 体验。

**ZCode 做法**：`useConversationTimelineFind.ts` + `conversationFindHighlightDom.ts`——在对话时间线里高亮匹配并逐条跳转。

**借鉴方案**：
- `Ctrl+F`（会话内）唤起搜索栏（浮动在对话区顶部或底部）
- 对已渲染的 message 文本做客户端全文匹配（虚拟列表已加载的行）
- 高亮匹配 + 上一个/下一个跳转（滚动到匹配行）
- 与 `useChatVirtualList` 配合：搜索触发时加载更多行（触发 `onReleaseHead` 的逆向——加载未在视口的行）

**改动清单**：
- [ ] **2.5.1** 新建 `client/app/composables/use-conversation-find.ts`：匹配 + 跳转 + 高亮状态
- [ ] **2.5.2** 新建 `client/app/components/chat/ConversationFindBar.vue`：搜索栏 UI（输入 + 上/下 + 匹配数/当前）
- [ ] **2.5.3** `ChatBox.vue`：集成搜索栏 + 高亮渲染（匹配文本包 `<mark>`）
- [ ] **2.5.4** 与虚拟列表协调：跳转到未加载行时触发分页加载

**工程量**：2-3 天
**验收**：F4 + F5（无匹配时不崩）。可与 §2.4 命令面板共享 QuickPick 基建。

---

## 3. 第二档：扎实提升、中等工程量

### 3.1 会话分支 / Fork

**差距**：sherry 明确没有分支（用折叠替代）。

**ZCode 做法**：`forkAssistant` 基于检查点追踪，feature id `conversation.history.branch`。

**借鉴方案**：与 §2.3 消息编辑共享 checkpoint 基建。fork = 从某 turn 复制一份会话快照，在新分支继续对话。依赖后端 checkpoint 能力。

**工程量**：5-7 天（依赖 §2.3 的后端 checkpoint）。建议排在 §2.3 之后。

---

### 3.2 全局快捷键系统 + 可重绑定的设置面板

**差距**：sherry 无快捷键面板；快捷键零散硬编码。

**ZCode 做法**：`shortcuts/bindings.ts` + `ShortcutSettingsSection.tsx`——支持重绑定、冲突检测、tooltip 自动跟随。

**借鉴方案**：统一快捷键层 + 右栏设置 tab（复用 RightSidebar）。可与 §2.4 命令面板一起做（命令注册表天然带快捷键字段）。

**改动清单**：
- [ ] **3.2.1** 新建 `client/app/composables/use-shortcuts.ts`：快捷键注册 + 监听 + 冲突检测
- [ ] **3.2.2** 新建 `client/app/pages/home/components/ShortcutSettingsPanel.vue`：右栏 tab，每行一个绑定 + 重绑输入 + 冲突提示
- [ ] **3.2.3** 迁移现有零散快捷键（Enter/Shift+Enter/Esc/↑↓）到统一层
- [ ] **3.2.4** 持久化（Dexie）

**工程量**：2-3 天（与 §2.4 合并做 4-5 天）

---

### 3.3 消息反馈（👍👎）+ 遥测

**差距**：sherry 无消息反馈。

**ZCode 做法**：每条 AI 消息 thumbs up/down，乐观更新 + 回滚，上报遥测。

**借鉴方案**：前端按钮 + 轻量后端上报接口。sherry 已有轨迹记录体系，反馈可挂到 trajectory_events 或独立表。

**改动清单**：
- [ ] **3.3.1** 后端：`POST /sessions/:id/feedback`，body `{turn_id, message_id, rating: "up"|"down", comment?}`
- [ ] **3.3.2** 前端：`ChatBox.vue` AI 消息行加 👍/👎 按钮（hover 显示，乐观更新 + 回滚）
- [ ] **3.3.3** 反馈状态持久化（已反馈的消息显示选中态）

**工程量**：1-2 天

---

### 3.4 PDF / Office 文档预览

**差距**：sherry 文件查看器（`FileViewerPanel.vue`）只有 markdown(v-safe-html) 或纯 `<pre>`。

**ZCode 做法**：`PreviewPane.tsx` 渲染 code/markdown/image/media/PDF/Office(docx/xlsx/pptx)。

**借鉴方案**：先接 PDF（pdf.js）和 docx（mammoth.js），xlsx/pptx 后续。

**改动清单**：
- [ ] **3.4.1** `FileViewerPanel.vue` 增格式分发：PDF → pdf.js 渲染器；docx → mammoth.js；其余维持现状
- [ ] **3.4.2** 新建 `PdfViewer.vue` + `DocxViewer.vue` 组件
- [ ] **3.4.3** 文件树右键菜单"在预览中打开"已有，无需改

**工程量**：3-4 天

---

### 3.5 侧边栏分组/文件夹 + 拖拽排序

**差距**：sherry 有标题/日期过滤，但无分组/文件夹/拖拽。

**ZCode 做法**：`project-sidebar/grouped/`——project/pinned/timeline 分组、粘性组头、拖拽覆盖层。

**借鉴方案**：加"置顶 + 自定义分组"。

**改动清单**：
- [ ] **3.5.1** 会话增 `group` 字段（Dexie 本地，不上后端）
- [ ] **3.5.2** `SessionSidebar.vue`：分组渲染 + 置顶区 + 拖拽排序（复用 PrimeVue 拖拽或自写）
- [ ] **3.5.3** 右键菜单：移入分组 / 置顶 / 取消置顶

**工程量**：3-4 天

---

### 3.6 引导 / Onboarding 流程

**差距**：sherry 无 onboarding，新用户面对九宫格 + 右栏一堆 tab 会迷茫。

**ZCode 做法**：`onboarding/`——欢迎页、职业网格、模式选择、AGENTS.md 迁移步骤。

**借鉴方案**：3-4 步引导（首次启动检测）。

**改动清单**：
- [ ] **3.6.1** 新建 `client/app/components/OnboardingDialog.vue`：PrimeVue `Dialog` 多步
- [ ] **3.6.2** 步骤：欢迎 → 选预设（复用 `NewSessionPresetDialog` 的内置预设）→ 指引核心操作（发消息/工具栏/右栏）
- [ ] **3.6.3** 首次启动检测（Dexie flag），完成后不再弹

**工程量**：2 天

---

### 3.7 语音输入到输入框

**差距**：sherry 后端 STT 已是可配模型族，但输入框未集成录音按钮。

**ZCode 做法**：`speech-input.tsx` + `voice-selector.tsx`。

**借鉴方案**：前端 `MediaRecorder` 录音 → base64 → 调后端 STT → 文本插入输入框。

**改动清单**：
- [ ] **3.7.1** `inputBox.vue` 增麦克风按钮（按住录音 / 点击录音）
- [ ] **3.7.2** 新建 `client/app/composables/use-voice-input.ts`：`MediaRecorder` + 调后端 STT API
- [ ] **3.7.3** 转录文本插入输入框光标位置

**工程量**：2-3 天

---

## 4. 第三档：有特色、工程量大或与 sherry 定位重叠

| ZCode 功能 | 评价与建议 |
|---|---|
| **多会话分屏（Split Pane）** | ZCode 的 `paneLayoutTree.ts` 支持任意分屏。Sherry 用 KeepAlive 缓存多会话 + 侧边会话已部分覆盖，分屏对屏幕空间要求高，优先级低。**暂不做。** |
| **插件市场 + 沙箱 UI + Gen-UI** | ZCode 全链路 `PluginStorePage` + 沙箱 + Gen-UI 实时调参。Sherry 的 MCP 面板目前是占位符。**长期方向，另起 TODO。** |
| **白板 / Canvas** | `WhiteboardPane.tsx` 手绘画板 + "加入对话"。Sherry 有知识图谱面板（`@antv/g6`）。**锦上添花，暂不做。** |
| **动态工作流 / Automations** | ZCode 定时/离峰工作流 + 运行历史 + 产物画布。Sherry 已有 Cron + Heartbeat + TaskFlow，功能有重叠。**增强而非照搬，评估后再定。** |
| **Web 远程控制（手机配对）** | ZCode 手机扫码配对 + 远程任务控制。Sherry 桌面优先定位。**暂不契合。** |
| **会话分享（只读链接）** | ZCode 一整套分享流程 + 独立 web-share 渲染。对团队协作有价值但工程量大（需后端分享链路）。**备选，看需求。** |
| **归档管理** | ZCode 软删除归档 + 批量清理。Sherry 只有硬删除。**低成本可加，归入 §3.5 分组的附带改进。** |
| **内联编辑 diff 预览** | ZCode 对 patch_file 工具调用显示彩色 diff 而非 JSON。Sherry 的 `ChatToolCard` 展示 JSON。**低成本改进，可独立做。** |
| **Assistant 预览卡片** | ZCode 自动打开 PPTX/HTML 预览。**依赖插件/Office 预览，暂不做。** |
| **设置远程同步** | ZCode settings-sync。**暂不契合。** |

---

## 5. 执行顺序与依赖

```
第一档（优先，立竿见影）：
  §2.2 轨迹调试视图（2 天，纯前端，后端已就绪）        ← 最先做，零后端依赖
  §2.1 代码高亮 + Mermaid + KaTeX（3-4 天，纯前端）     ← 体验缺口最大
  §2.4 命令面板 + §2.5 会话内搜索（合并 4-5 天）         ← 共享 QuickPick 基建
  §2.3 消息编辑/重生成（阶段一 4-5 天）                  ← 核心交互，后端需小改

第二档（扎实提升）：
  §3.3 消息反馈 👍👎（1-2 天）                          ← 低成本遥测
  §3.2 快捷键系统（与 §2.4 合并）
  §3.4 PDF/docx 预览（3-4 天）
  §3.7 语音输入（2-3 天）
  §3.6 引导流程（2 天）
  §3.5 侧边栏分组 + 归档（3-4 天）

第三档（长期/备选）：
  §2.3 阶段二 工作区回退（3-4 天，依赖 shadow-git）
  §3.1 会话分支 Fork（5-7 天，依赖 §2.3 checkpoint）
  会话分享、插件市场等（另起 TODO）
```

**关键路径**：§2.2 零依赖最先做；§2.4 命令面板是 §3.2 快捷键系统的基建，合并做；§2.3 消息编辑是 §3.1 分支的前置。

**总工程量估算**：
- 第一档：约 13-16 天
- 第二档：约 11-15 天
- 第三档：按需另估

---

## 6. 验收标准

### 6.1 硬约束验收

- [ ] **F1**：老前端访问新后端不崩（新增 frame 是追加）。
- [ ] **F2**：现有 message store schema 不变。
- [ ] **F3**：shiki/math/mermaid 输出经 DOMPurify 扩展 allowlist 或沙箱，无裸 HTML 注入。
- [ ] **F4**：新功能 additive，现有渲染 byte-identical。
- [ ] **F5**：数据缺失/组件加载失败时退化到线性展示，不崩。
- [ ] **F6**：不依赖 LLM 改输出格式。

### 6.2 各功能验收（按完成项勾选）

- [ ] §2.1 代码块高亮 + Mermaid + KaTeX：代码块带高亮 + 工具栏；Mermaid 沙箱渲染；公式渲染；fail-open 退化纯文本。
- [ ] §2.2 轨迹调试视图：右栏新 tab；调现有 `GET /sessions/:id/trajectory`；时间线 + 搜索。
- [ ] §2.3 消息编辑：编辑用户消息 → 截断重跑；superseded 消息可恢复。
- [ ] §2.4 命令面板：`Ctrl+K` 唤起；模糊匹配 + 搜索历史；覆盖会话/设置/工具栏命令。
- [ ] §2.5 会话内搜索：`Ctrl+F` 高亮 + 上下跳转；与虚拟列表协调。
- [ ] §3.1-§3.7 各项按子任务清单验收。

### 6.3 回归验收

- [ ] 现有 `tests/agent/middlewares` + `tests/server` 全套通过。
- [ ] 现有 `client/` 单元/集成测试通过（`pnpm test:unit && pnpm test:integration`）。
- [ ] 现有 ChatBox 渲染在未启用增强时 byte-identical。

---

## 7. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 移植 ZCode 代码 | 技术栈不同（React vs Vue），只抄交互范式，重写实现 |
| 改 message store schema | 消息编辑的截断是逻辑标记（superseded），不改 schema |
| 强制 LLM 改输出格式 | 所有增强是前端 + 协议层做的，不依赖 LLM 配合 |
| 多会话分屏 | KeepAlive + 侧边会话已部分覆盖，屏幕空间成本高，暂不做 |
| 插件市场 / Gen-UI | 全链路长期方向，另起 TODO |
| Web 远程控制 | 与 sherry 桌面优先定位不契合 |
| 本 TODO 已覆盖的 turn 折叠 | 已在 `计划_借鉴ZCode_turn折叠展示.md` 实现完成 |

---

## 8. 关键参考

### sherry 内部（复用/改造）

- `server/service/trajectory_store.py`（轨迹账本——§2.2 后端已就绪）
- `server/trigger/http/trajectory.py`（`GET /sessions/:id/trajectory`——§2.2 调用）
- `client/app/constants/security.ts`（DOMPurify allowlist——§2.1 扩展对象）
- `client/app/pages/home/components/ChatBox.vue`（对话渲染——§2.1/§2.3/§2.5 改造对象）
- `client/app/components/chat/inputBox.vue`（输入框——§2.3/§3.7 改造对象）
- `client/app/composables/mitt.ts`（事件总线——§2.4 命令分发复用）
- `client/app/pages/home/components/RightSidebar.vue` + `dialogs.ts`（右栏 tab 宿主——§2.2/§3.2 注册对象）
- `client/app/pages/home/components/FileViewerPanel.vue`（文件查看器——§3.4 改造对象）
- `client/app/pages/home/components/SessionSidebar.vue`（侧边栏——§3.5 改造对象）
- `workspace/.sherry/shadow-git/`（per-project write snapshots——§2.3 阶段二复用）

### ZCode 参考（对齐目标）

- `ZCode-main/.../packages/ui/src/components/ai-elements/code-block.tsx`（Shiki 高亮 + 工具栏——§2.1）
- `ZCode-main/.../packages/ui/src/components/ai-elements/mermaid-block.tsx` + `diagram-preview-dialog.tsx`（Mermaid——§2.1）
- `ZCode-main/.../packages/ui/src/ModelTrajectoryPane.tsx` + `ModelTrajectoryTimeline.tsx` + `ModelTrajectorySearchBar.tsx`（轨迹调试——§2.2）
- `ZCode-main/.../packages/ui/src/command-center/CommandCenterDialog.tsx` + `quickpick/TaskFindDialog.tsx`（命令面板 + 会话内搜索——§2.4/§2.5）
- `ZCode-main/.../packages/ui/src/v4/ConversationRowView.tsx` 的 `SingleUserInputRowView`（消息编辑——§2.3）
- `ZCode-main/.../packages/ui/src/ConversationFileRewindDialog.tsx`（工作区回退——§2.3 阶段二）
- `ZCode-main/.../packages/ui/src/settings/ShortcutSettingsSection.tsx` + `shortcuts/bindings.ts`（快捷键系统——§3.2）
- `ZCode-main/.../packages/ui/src/components/ai-elements/speech-input.tsx`（语音输入——§3.7）
- `ZCode-main/.../packages/ui/src/onboarding/`（引导流程——§3.6）
- `ZCode-main/.../packages/ui/src/project-sidebar/grouped/`（侧边栏分组——§3.5）

### 关联 TODO

- `计划_借鉴ZCode_turn折叠展示.md`（已实现，正交）
- `计划_工具合并推荐方案.md`（工具面瘦身，正交）
