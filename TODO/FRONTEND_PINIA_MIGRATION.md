# 前端 Pinia 状态迁移方案

## 现状

Pinia 仅 1 个 store（`stores/ui.ts`，3 个状态），~45 个模块级 ref 散落在 18 个 composable 文件里充当单例。

| 状态类型                 | 位置                 | 数量                                                       |
| ------------------------ | -------------------- | ---------------------------------------------------------- |
| Pinia store              | `stores/ui.ts`       | 3（sidebarCollapsed, settingsMenuOpen, todoDockCollapsed） |
| 模块级 ref（响应式状态） | 7 个 composable 文件 | ~22                                                        |
| 模块级 ref（基础设施）   | 6 个 composable 文件 | ~23                                                        |
| 组件内 ref               | 各组件               | ~19                                                        |

## 迁移边界

**迁移**：响应式 UI 状态 → Pinia store
**保留**：基础设施单例（WebSocket 实例、事件总线、Dexie、toast API）留模块级

| 保留模块级        | 原因                         |
| ----------------- | ---------------------------- |
| `ws.ts`           | WebSocket 实例不是响应式状态 |
| `mitt.ts`         | 事件总线，不是状态           |
| `db.ts`           | Dexie 实例，不是状态         |
| `agent-socket.ts` | socket 连接 Map，基础设施    |
| `toast.ts`        | 注入的 PrimeVue API 引用     |
| `clientLog.ts`    | 日志缓冲 + 订阅者，基础设施  |

---

## 新建 4 个 Pinia Store

### 1. `stores/subagent.ts`

合并 `subagent-state.ts` + `subagent-tree.ts` + `subagent-selection.ts` + `subagent-sync.ts`

**State**:

```
taskRuns, allTaskRuns, taskLoading, lastTasksFetchedAt,
subagentWsReady, tasksTabActive,
expandedRunId, selectedRunId, focusedRunId,
selectedRunIds, deletingRunIds,
subagentValidSessionIds
```

**Getters**:

```
runningTaskCount, allRunningTaskCount,
rootTaskRuns, groupedRootTaskRuns, focusedSubtreeRuns,
allSelected, someSelected, selectableRunIds
```

**Actions**:

```
setTasksTabActive(active)
toggleExpandRun(runId)
focusRun(runId)
resetFlowState()
toggleTaskSelection(runId)
toggleSelectAllTasks()
clearTaskSelection()
async deleteSubagentSubtree(runId)
async deleteSelectedTasks()
```

**WS 订阅留 composable**：`subagent-sync.ts` 的 `initSubagentSync()` / `loadSubagentTasks()` 等函数保留，内部改为 `const store = useSubagentStore()` + 调用 store actions。

### 2. `stores/connection.ts`

从 `connection.ts` 迁移

**State**:

```
isOnline, backendStatus, lastReachable
```

**Actions**:

```
startConnectionWatch()
stopConnectionWatch()
```

**保留模块级**：`clientFlagOverride`（toast.ts 也用，共享工具提取到 `utils/client.ts`）

### 3. `stores/todo.ts`

从 `use-todo-list.ts` 迁移

**State**:

```
todos: Todo[]
currentSid: string | undefined
subscribed: boolean
```

**Getters**:

```
doneCount, totalCount,
linkedTodos (有 flow_id 的), unlinkedTodos (无 flow_id 的),
flowGroups (按 flow_id 分组)
```

**Actions**:

```
setTodos(todos)
setCurrentSid(sid)
subscribe()
unsubscribe()
async refreshTodos()
```

**WS 推送处理**：`mitt.ts` 的 `ws:todo_updated` 事件监听器调用 `store.setTodos()`，逻辑不变。

### 4. `stores/chat-background.ts`

从 `useChatBackground.ts` 迁移

**State**:

```
backgroundUrl: string | null
backgroundOpacity: number
backgroundLoaded: boolean
```

**Getters**:

```
chatBackgroundStyle  (需 useColorMode，在 store setup 内捕获)
chatBackgroundOverlayStyle
```

**Actions**:

```
async loadBackground()
setBackground(url, opacity)
clearBackground()
```

**注意**：`useColorMode()` 依赖 Nuxt setup context，必须在 store setup 函数体内调用（和 `ui.ts` 一样）。

---

## Composable 降级为薄封装

原 composable 文件保留导出函数签名，内部改为读 store：

```typescript
// subagent-state.ts — before
export const taskRuns = ref<SubagentRun[]>([]);

// after
import { useSubagentStore } from "~/stores/subagent";
export function useSubagentState() {
  const store = useSubagentStore();
  return {
    taskRuns: computed(() => store.taskRuns),
    // ...
  };
}
```

但大部分调用方直接 import ref 的地方需要改为函数调用。权衡后建议**直接删掉原 composable 文件**，调用方改为 `useXxxStore()`。

---

## 迁移步骤（按依赖顺序）

### Step 1: `stores/subagent.ts`（最大块，先做）

| 改动                 | 文件                                                                                                         |
| -------------------- | ------------------------------------------------------------------------------------------------------------ |
| 新建 store           | `client/app/stores/subagent.ts`                                                                              |
| 改 composable 调用方 | `SubagentTasksView.vue`, `SubagentFlowGraph.vue`, `useSubagentTasks.ts`                                      |
| 改 WS 订阅           | `subagent-sync.ts` 内部改为调 store actions                                                                  |
| 改测试               | `tests/` 中 import 模块级 ref 的地方改为 `useSubagentStore()`                                                |
| 删除旧文件           | `subagent-state.ts`, `subagent-tree.ts`, `subagent-selection.ts` 合并后删（`subagent-sync.ts` 保留 WS 逻辑） |

### Step 2: `stores/todo.ts`

| 改动                 | 文件                                                |
| -------------------- | --------------------------------------------------- |
| 新建 store           | `client/app/stores/todo.ts`                         |
| 改 composable 调用方 | `TodoDock.vue`, `TodoItem.vue`, `[sid].vue`         |
| 改测试               | `__tests__/use-todo-list.test.ts`                   |
| 删除旧文件           | `use-todo-list.ts` 的状态部分迁入 store，函数签名留 |

### Step 3: `stores/connection.ts`

| 改动                 | 文件                              |
| -------------------- | --------------------------------- |
| 新建 store           | `client/app/stores/connection.ts` |
| 改 composable 调用方 | `home/index.vue`, `app.vue`       |
| 改测试               | `__tests__/connection.test.ts`    |

### Step 4: `stores/chat-background.ts`

| 改动                 | 文件                                   |
| -------------------- | -------------------------------------- |
| 新建 store           | `client/app/stores/chat-background.ts` |
| 改 composable 调用方 | `home/index.vue`, `ConfigDialog.vue`   |
| 改测试               | `__tests__/useChatBackground.test.ts`  |

---

## 测试策略

```typescript
// Pinia 测试：createTestingPinia 自动 mock actions
import { createTestingPinia } from "@pinia/testing";

beforeEach(() => {
  setActivePinia(createTestingPinia());
});

test("toggleExpandRun", () => {
  const store = useSubagentStore();
  store.toggleExpandRun("run-1");
  expect(store.expandedRunId).toBe("run-1");
});
```

比原来的模块级 ref 测试更简单 — 不需要手动 reset 模块状态。

---

## 不改的部分

| 组件              | 原因                           |
| ----------------- | ------------------------------ |
| `ws.ts`           | WebSocket 实例管理，基础设施   |
| `mitt.ts`         | 事件总线                       |
| `db.ts`           | Dexie 实例                     |
| `agent-socket.ts` | socket 连接                    |
| `toast.ts`        | 注入 API                       |
| `clientLog.ts`    | 日志缓冲                       |
| 组件内 ref        | 局部 UI 状态，不需要跨组件共享 |

---

## 预估工作量

| Step     | 内容                        | 预估       |
| -------- | --------------------------- | ---------- |
| 1        | `stores/subagent.ts`        | 1 天       |
| 2        | `stores/todo.ts`            | 0.5 天     |
| 3        | `stores/connection.ts`      | 0.5 天     |
| 4        | `stores/chat-background.ts` | 0.5 天     |
| 测试     | 全量改 + 新测试             | 1 天       |
| **合计** |                             | **3.5 天** |
