# 计划_TaskFlow看板与实时Trajectory_20261104

实现三个缺失的实时 UI：TaskFlow 看板步骤状态、WS task table 前端渲染、
subagent 子会话实时 trajectory/工具调用流。

---

## 一、TaskFlow 看板式步骤状态 UI

### 现状

后端完整、前端有数据无渲染：
- `agent/tools/taskflow/progress_push.py` 构建 board payload（`flow_id`/`status`/
  `description`/`total`/`done`/`by_status`/`steps[]`），通过 session WS
  `taskflow_updated` 帧推送 + `taskflow_refresh` 拉取（`server/trigger/core.py:193-216`）。
- `client/app/stores/taskflow.ts` 已接收并存储 `flows`/`totals`/`hasProgress`/
  `inFlight`/`started`/`percent`，但**无任何组件渲染这些数据**。
- StepStatus 枚举：`blocked` → `ready` → `dispatched` → `done`（`dispatched` = 子代理正在执行）。

### 落地步骤

#### 1. 新建 `TaskFlowBoardPanel.vue`

位置：`client/app/pages/home/components/TaskFlowBoardPanel.vue`

布局（参照看板风格，sherry 暗色主题）：
```
┌─ TaskFlow Board ────────────────────────────┐
│  总进度: ████████░░ 8/12  (3 flows active)   │
├──────────────────────────────────────────────┤
│  Flow: <description>                         │
│  Status: RUNNING   Steps: 4/6                │
│  ┌─────────┬─────────┬─────────┬──────────┐ │
│  │ Blocked │ Ready   │Dispatch │  Done    │ │
│  │ step-1  │ step-2  │ step-3  │  step-4  │ │
│  │ step-5  │         │ (running│  step-6  │ │
│  │         │         │  spinner│          │ │
│  └─────────┴─────────┴─────────┴──────────┘ │
│  ○ blocked  ○ ready  ◎ dispatched  ● done   │
└──────────────────────────────────────────────┘
```

- 读取 `useTaskFlowStore()` 的 `flows` + `totals`。
- 每个 flow 渲染为一列，steps 按状态分泳道（`STATUS_ORDER`:
  blocked/ready/dispatched/done/failed/skipped/cancelled）。
- `dispatched` 步骤显示 spinner 动画（子代理正在工作）。
- `failed` 步骤红色高亮，`skipped`/`cancelled` 灰色删除线。
- 步骤卡片显示 `step_id` + `task`（clipped 200 chars）。
- 无 active flow 时显示空态提示。

#### 2. 注册为右侧栏标签页

- `RightSidebar.vue` PANELS map 加 `taskFlowBoard: defineAsyncComponent(...)`。
- `stores/right-sidebar.ts` 加 `taskFlowBoard: 'session'`（当前会话 scope），
  加入 `RightSidebarPanelKind` union。
- i18n：`rightSidebar.taskFlowBoard` = "任务看板"/"Task Board"/"タスクボード"/"작업 보드"，
  四语言中央 locale + RightSidebar 内联。

#### 3. 响应式更新

store 已在 `ws:taskflow_updated` mitt 事件时更新（`stores/taskflow.ts:80-101`），
组件用 `computed` 从 store 读 `flows`，自动响应。无需额外轮询。

#### 4. 测试

- `TaskFlowBoard.integration.test.ts`：mock store flows，验证泳道分组、
  spinner 显示、空态、进度条百分比。
- store 测试已有（`stores/__tests__/taskflow.test.ts`），补充渲染断言。

### 涉及文件

| 文件 | 操作 |
|------|------|
| `components/TaskFlowBoardPanel.vue` | 新建 |
| `components/RightSidebar.vue` | 加 PANELS entry + i18n |
| `stores/right-sidebar.ts` | 加 panel kind + scope |
| `i18n/locales/{zh,en,ja,ko}.json` | 加 tab 标签 |

---

## 二、WS task table 前端渲染

### 现状澄清

"WS task table"（`runtime/hooks.py::WS_ACTIVE_TASKS`）是内部 `dict[str, asyncio.Task]`
——session 的 WebSocket 流协程表，**不是 UI 数据**。它只用于 subagent session-state
判断"session X 有没有活跃 socket"。用户实际需要看到的是 TaskFlow board + subagent
运行列表的组合视图，而非这个内部表。

因此本节的落地是：**不直接渲染 asyncio.Task 表**，而是构建一个统一的"会话活动总览"
面板，聚合 TaskFlow board + subagent 运行记录 + 前台工具调用，作为 session 级实时活动面板。

### 落地步骤

#### 1. 新建 `SessionActivityPanel.vue`

位置：`client/app/pages/home/components/SessionActivityPanel.vue`

三个折叠区块（参照 ZCode `ConversationStatusPanel` 的折叠 section 设计）：

1. **任务流（TaskFlow）**：内嵌 `TaskFlowBoardPanel` 的精简版（仅活跃 flow 的步骤进度条，
   不展开泳道），点击展开跳转到完整看板 tab。
2. **子代理（Subagents）**：活跃子代理列表（status chip + name + elapsed timer），
   数据来自 `stores/subagent.ts::taskRuns` filtered by `isActiveRun`。
   点击某行跳转到右侧栏 subagent 实况 tab（见计划 C）。
3. **前台工具（Foreground Tools）**：当前正在执行的前台工具调用列表，
   数据来自 `stores/commands.ts`（`TasksButton.vue` 已读取的同一数据源）。

#### 2. 注册与入口

- 右侧栏 tab `sessionActivity`，scope `session`。
- 或作为 TasksButton popover 的增强版（popover 内容替换为内嵌此面板的精简版）。

### 涉及文件

| 文件 | 操作 |
|------|------|
| `components/SessionActivityPanel.vue` | 新建 |
| `components/RightSidebar.vue` | 加 PANELS entry + i18n |
| `stores/right-sidebar.ts` | 加 panel kind + scope |

---

## 三、Subagent 子会话实时 Trajectory / 工具调用流

### 现状

- `server/service/trajectory_store.py`：按 `(session_id, turn_id, seq)` 存储，
  事件种类 `tool_start`/`tool_end`/`turn_start`/`turn_end`/`compact`/`model_retry`/
  `error`。子代理会话有独立 trajectory（`current_turn_id` contextvar 隔离）。
- `GET /sessions/:sid/trajectory`：HTTP 轮询，无 WS 推送。
- 前端 `TrajectoryPanel.vue`：HTTP fetch，mount 时拉取 + 手动刷新，无实时推送。
- `server/trigger/ws/` 无 trajectory channel。

### 落地步骤

#### 1. 后端：新建 trajectory WS 推送通道

新建 `server/trigger/ws/trajectory_ws.py`：
- 端点 `ws://.../sessions/:session_id/trajectory/ws`。
- 在 `trajectory_store.py::record_event` 中（或其调用后）增加一个 hook 调用，
  将事件通过 `runtime.hooks` 推送到该 session 的 trajectory WS 订阅者。
- 帧格式：`{"event": "trajectory_event", "session_id", "data": {event_id, turn_id, seq, ts, kind, is_error, summary, payload}}`。
- 连接时发送一个 `ready` 帧 + 当前 turn 的历史事件快照（gap-fill）。
- 订阅按 `session_id` 路由（`broadcast_to_session` 或独立 channel）。

注册 hook：
- `runtime/hooks.py` 加 `TRAJECTORY_EVENT_PUSH = "trajectory_event_push"`，
  签名 `(session_id: str, event: dict) -> None`。
- `trajectory_store.py::record_event` 末尾调用 `hooks.call(TRAJECTORY_EVENT_PUSH, ...)`，
  fail-open。
- `server/__main__.py` 注册推送实现，写入对应 session 的 trajectory WS 订阅者集合。

#### 2. 后端：子代理 trajectory 推送到父会话

子代理的 trajectory 事件在其自己的 `session_id` 下记录。为了让父会话的
实时视图也能看到子代理活动，有两种方案：

**方案 A（推荐）**：trajectory WS 支持订阅子代理 session_id。
前端在打开 subagent 实况 tab 时，用子代理的 `child_session_key` 连接
`/sessions/<child_session_key>/trajectory/ws`，直接订阅子代理的事件流。

**方案 B**：子代理事件也推送到父会话的 trajectory channel（带 `child_session_id` 标记）。
前端在父会话的 trajectory 流中过滤子代理事件。但会增加父会话噪音。

采用方案 A——每个 session 独立订阅，前端按需连接。

#### 3. 前端：trajectory 实时 composable

新建 `client/app/composables/use-trajectory-stream.ts`：
```ts
export function useTrajectoryStream(sessionId: Ref<string>) {
  const events = ref<TrajectoryEvent[]>([]);
  const connected = ref(false);
  // 连接 /sessions/<sid>/trajectory/ws
  // on 'trajectory_event' → push to events
  // on 'ready' → gap-fill snapshot
  // 断线重连
  return { events, connected };
}
```

- 复用 `composables/ws.ts` 的 WebSocket 管理模式（或新建独立连接）。
- 事件按 `(turn_id, seq)` 排序，`tool_start`/`tool_end` 按 `event_id` 合并
  （tool_start + tool_end = 一个完整工具调用的 begin + result）。

#### 4. 前端：subagent trajectory 渲染组件

新建 `client/app/pages/home/components/SubagentTrajectory.vue`：
- 接收 `sessionId` prop（子代理的 child_session_key）。
- 调用 `useTrajectoryStream(ref(sessionId))` 获取实时事件流。
- 渲染为时间线：
  ```
  ┌─ Subagent Trajectory ─────────────────┐
  │  Turn: <turn_id>                      │
  │  ● turn_start                         │
  │  ├─ ◎ tool_start: read_file           │
  │  │   └─ ✓ tool_end: 120ms             │
  │  ├─ ◎ tool_start: patch_file          │
  │  │   └─ ⏳ running...                  │
  │  ● turn_end: 340ms                    │
  └───────────────────────────────────────┘
  ```
- `tool_start` 显示工具名 + 参数预览，状态 spinner。
- `tool_end` 显示耗时 + 结果预览（clipped），成功 ✓ / 失败 ✗。
- `turn_start`/`turn_end` 作为时间线分隔。
- `error`/`model_retry`/`compact` 特殊标记。
- 事件按 turn 分组，每个 turn 可折叠。

#### 5. 集成到 subagent 实况 tab

此组件嵌入计划 C 的 `SubagentActivityPanel.vue`（右侧栏 subagent 实况 tab），
作为其中的一个 tab/section。

### 涉及文件

| 文件 | 操作 |
|------|------|
| `server/trigger/ws/trajectory_ws.py` | 新建 |
| `runtime/hooks.py` | 加 `TRAJECTORY_EVENT_PUSH` hook |
| `server/service/trajectory_store.py` | `record_event` 末尾调 hook |
| `server/__main__.py` | 注册 trajectory push 实现 |
| `client/app/composables/use-trajectory-stream.ts` | 新建 |
| `client/app/pages/home/components/SubagentTrajectory.vue` | 新建 |
| `server/trigger/ws/trajectory_ws.py` 测试 | 新建 |
| `tests/` trajectory push 测试 | 新建 |

### 注意事项

- **性能**：trajectory 事件每 turn 3-12 条，单 session 的流量很小。
  WS 推送比 HTTP 轮询更高效。
- **子代理 session 生命周期**：子代理结束后 trajectory WS 自动断开
  （前端在 tab 关闭 / run ended 时主动断开）。
- **与现有 TrajectoryPanel 的关系**：主会话的 `TrajectoryPanel.vue` 可后续迁移到
  WS 推送，但本次只做子代理的实时流（主会话可保持 HTTP 轮询，作为渐进迁移）。
- **hook fail-open**：trajectory push hook 失败不影响 trajectory_store 的写入
  （`try/except` 包裹）。

---

## 优先级与依赖

1. **先做 TaskFlow 看板**（第一部分）——后端数据已就绪，纯前端工作，无依赖。
2. **再做 subagent trajectory WS**（第三部分）——需要后端新建推送通道，
   是计划 C（subagent 实况页）的前置依赖。
3. **最后做 SessionActivityPanel**（第二部分）——聚合视图，依赖前两者。
