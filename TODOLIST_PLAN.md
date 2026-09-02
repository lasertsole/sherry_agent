# sherry_agent TodoList 实现方案

> 参考项目：opencode-dev (D:\selfProj\opencode-dev) + oh-my-openagent-dev (D:\selfProj\oh-my-openagent-dev)
> 日期：2026-09-02

## 设计哲学

```
opencode:     简单（1 工具，无保护）          → 压缩后丢失 ❌
oh-my-openagent: 健壮（5 层防御，600+ 行修复代码）  → 过重 ❌
sherry_agent:  折中（2 工具 + 系统提示词注入）    → 轻量且无损 ✅
```

核心思路：sherry_agent 的 `build_system_prompt()` 在每次上下文压缩后都会被 `ContextEngineHook` 重新调用。如果在这个重建点注入当前 todos，todo 状态就天然免疫压缩——无需 oh-my-openagent-dev 的快照/恢复/拦截 5 层复杂机制。

## 七层架构

```
Layer 7  │ UI 组件层        │ TodoDock.vue + TodoItem.vue (PrimeVue)
Layer 6  │ 前端状态层        │ useTodoList.ts (模块级单例)
Layer 5  │ 实时通信层        │ WS: todo_updated 事件 → mitt 分发
Layer 4  │ 压缩保护层 ★     │ build_system_prompt 注入当前 todos
Layer 3  │ 工具层           │ todowrite (全量替换) + todoread (读回)
Layer 2  │ 服务层           │ TodoStore: replace_all / get_todos
Layer 1  │ 数据存储层        │ todos.db (独立, WAL)
```

---

## Layer 1: 数据存储层

- **新建文件**: `agent/tools/todolist/registry/store_sqlite.py`
- **DB 路径**: `agent/tools/todolist/data/todos.db`

复用 taskflow 的 SQLite 模式（WAL + busy_timeout + ensure_db + 事件循环安全），但去掉乐观锁（单写者无需）。

### 表结构

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
CREATE INDEX IF NOT EXISTS idx_todos_session ON todos(session_id);
```

### E6 字段说明（委派与 Fan-out，详见 TODOLIST_ENFORCEMENT.md）

| 字段          | 默认值  | 可选值                                               | 说明                                              |
| ------------- | ------- | ---------------------------------------------------- | ------------------------------------------------- |
| `category`    | `quick` | `quick`/`deep`/`ultrabrain`/`visual`/`git`/`writing` | 路由裁决，告诉 LLM 该 todo 应由哪类 subagent 执行 |
| `delegation`  | `self`  | `self`/`subagent`                                    | 委派策略                                          |
| `subagent_id` | `NULL`  | nullable string                                      | 关联的 subagent child_session_key                 |

### CRUD 接口

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """全量替换：DELETE + INSERT（事务）"""

async def get_todos(session_id: str) -> list[dict]:
    """按 position 排序读取"""

def get_todos_sync(session_id: str) -> list[dict]:
    """同步路径，用于系统提示词注入（无事件循环场景）"""
```

### 比 taskflow 简化的地方

- 无 `expected_revision` / 乐观锁 / `FlowConflictError`
- 无 `state_json` 大 JSON / `wait_json` / `child_session_key`
- 无 `_Unset` 哨兵
- 异常类只需 `TodoStoreError`（基类）+ `TodoNotFoundError`

### 参考文件

- 完整 SQLite 模式参考: `agent/tools/todolist/registry/store_sqlite.py`（taskflow 版 389 行）
- 连接生命周期: `_connect()` async context manager + `PRAGMA busy_timeout = 5000`
- WAL 切换: `_switch_to_wal_if_needed()` 容错
- 事件循环安全初始化: `ensure_db()` + `_initialized` + `_init_lock` + `_init_loop`
- 同步路径: `threading.Lock` + stdlib `sqlite3`
- 测试隔离: monkeypatch `_DB_PATH` + 重置初始化标志

---

## Layer 2: 服务层

- **新建文件**: `agent/tools/todolist/service.py`

### 接口

```python
async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
    """全量替换 + WS 推送 + 返回最新列表"""
    validated = _validate_todos(todos)  # 校验 status/priority/category/delegation 枚举
    # E6d: 转换屏障 — subagent 仍在运行时禁止标记 completed
    for todo in validated:
        if todo["status"] == "completed" and todo.get("subagent_id"):
            if await _is_subagent_running(todo["subagent_id"]):
                raise TodoStoreError(
                    f"Cannot mark todo completed: subagent {todo['subagent_id']} "
                    "is still running. Wait for it to finish first."
                )
    await store.replace_all(session_id, validated)
    latest = await store.get_todos(session_id)
    await _push_todo_update(session_id, latest)  # WS 推送
    return latest

async def get_todos(session_id: str) -> list[dict]:
    return await store.get_todos(session_id)

# E6d: subagent 运行检查（Phase 2 代码级屏障）
async def _is_subagent_running(child_session_key: str) -> bool:
    from agent.tools.subagent.registry import get_run_by_child_session_key, has_run_ended
    run = get_run_by_child_session_key(child_session_key)
    if run is None:
        return False
    return not has_run_ended(run)
```

### WS 推送（复用 sherry_agent 已有的 Pattern A — relation_register 直发）

```python
async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    from runtime import relation_register
    import json
    ws = relation_register.get_websocket_by_session_id(session_id)
    if ws:
        await ws.send_text(json.dumps({
            "event": "todo_updated",
            "session_id": session_id,
            "content": {"todos": todos}
        }))
```

参考实现: `server/service/heartbeat.py` 的 `push_heartbeat_updated()` 用完全相同的模式。

### WS 推送策略：WS 重连时主动重发（不需要 HTTP REST API）

不需要 `GET /sessions/:id/todos` 端点。前端 WS 重连后，通过 WS 发一条消息让后端重发当前 todos。

理由：

- 不引入 HTTP 路由，全链路走 WebSocket
- 重连后 UI 能立即恢复最新状态，不用等 LLM 下次 todowrite
- 后端收到请求后从 DB 读取并推送，逻辑简单

### 前端请求重发

```typescript
// useTodoList.ts 中
on("ws:reconnected", () => {
  // 通过 WS 发一条消息让后端重发 todos
  emit("ws:send", { type: "todo_refresh", session_id: currentSessionId.value });
});
```

```typescript
// ws.ts 中新增发送能力
export function sendWs(payload: Record<string, any>): void {
  if (wsInstance?.readyState === WebSocket.OPEN) {
    wsInstance.send(JSON.stringify(payload));
  }
}

// mitt 监听 ws:send 事件
on("ws:send", (payload) => sendWs(payload));
```

### 后端处理重发请求

在通用 WS handler（`server/trigger/core.py` 的 `/sessions/ws`）中新增 `todo_refresh` 消息处理：

```python
# 收到 {type: "todo_refresh", session_id: "xxx"} 后
async def handle_todo_refresh(session_id: str, websocket):
    from agent.tools.todolist.service import get_todos
    todos = await get_todos(session_id)
    await websocket.send_text(json.dumps({
        "event": "todo_updated",
        "session_id": session_id,
        "content": {"todos": todos}
    }))
```

---

## Layer 3: 工具层

### 为什么加 todoread（opencode 没有，这是改进）

opencode 的设计缺陷就是没有 `todoread`。sherry_agent 虽然有系统提示词注入（Layer 4），但 `todoread` 作为显式安全网——LLM 不确定时可以主动调用，零成本。

### todowrite.py — 全量替换，写即读

- **新建文件**: `agent/tools/todolist/tools/todowrite.py`

```python
@tool("todowrite")
async def todowrite(
    todos: list[dict],  # [{content, status, priority, category?, delegation?, subagent_id?}]
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Update the todo list for the current session (full replacement).

    Pass the COMPLETE list every time. Status: pending|in_progress|completed|cancelled.
    Priority: high|medium|low.
    Category (optional): quick|deep|ultrabrain|visual|git|writing.
    Delegation (optional): self|subagent.
    Subagent_id (optional): child_session_key returned by task tool.
    """
    if not todos:
        return "Error: todos list is empty. Pass an empty list [] to clear all todos."
    try:
        result = await service.update_todos(session_id, todos)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        # E6a: fan-out reminder（每 session 首次触发）
        if session_id not in _reminded_sessions:
            _reminded_sessions.add(session_id)
            output += _FANOUT_REMINDER
        return output
    except Exception as e:
        return f"Error: {e}"
```

### todoread.py — 显式读取（安全网）

- **新建文件**: `agent/tools/todolist/tools/todoread.py`

```python
@tool("todoread")
async def todoread(
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Read the current todo list from database. Use when you're unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### 工具注册

- **新建文件**: `agent/tools/todolist/tools/__init__.py`

```python
_TODOLIST_TOOLS = [todowrite, todoread]

def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    return list(_TODOLIST_TOOLS)
```

- **修改文件**: `agent/tools/__init__.py` — 在 `_MAIN_TOOLS_BUILDERS` 中加入 `build_todolist_tools`

### 错误处理契约（参考 taskflow/tools/_shared.py）

- 工具永不向 LLM 抛业务异常，一律返回 `"Error: ..."` 前缀的可读字符串
- `handle_tool_error = True` 作为兜底

### SKILL.md

- **新建文件**: `skills/todolist/SKILL.md`

```markdown
---
name: todolist
description: Lightweight session-scoped task tracking visible to the user. Use for 3+ step work.
scope: main_only
---

# TodoList — 任务跟踪面板

## 何时使用

- 开始 3+ 步骤的复杂工作时，创建 todo 列表
- 每完成一步，更新状态（pending -> in_progress -> completed）
- 全部完成后，用空列表 [] 清除

## 工具

- `todowrite(todos)`: 全量替换当前 todo 列表（每次传完整列表）
- `todoread()`: 从数据库读取当前列表（不确定状态时使用）

## 状态: pending | in_progress | completed | cancelled

## 优先级: high | medium | low

## 委派字段 (E6, 可选):

- category: quick|deep|ultrabrain|visual|git|writing — 路由裁决
- delegation: self|subagent — 是否委派给 subagent
- subagent_id: 派出 subagent 后填入 child_session_key

## 规则

- 每次调用 todowrite 传入完整列表，不是增量更新
- 同时只有一个 in_progress 任务
- delegation="subagent" 时，subagent 返回前不得标记 completed
```

### 参考文件

- @tool 模式: `agent/tools/taskflow/tools/taskflow_create.py`
- session_id 注入: `agent/tools/taskflow/tools/taskflow_run_task.py`（`Annotated[str, InjectedState("session_id")]`）
- 错误处理: `agent/tools/taskflow/tools/_shared.py`

---

## Layer 4: 压缩保护层 ★（核心创新）

- **修改文件**: `workspace/prompt_builder.py`

这是解决 opencode 设计缺陷的关键层。sherry_agent 的 `build_system_prompt()` 在每次压缩后都会被 `ContextEngineHook` 重新调用——如果在这里注入当前 todos，就天然免疫压缩。

### 实现

```python
# workspace/prompt_builder.py — build_system_prompt() 中新增

def _build_todo_block(session_id: str) -> str:
    """从 DB 读取当前 todos，注入系统提示词"""
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        cat = t.get("category", "quick")
        dlg = t.get("delegation", "self")
        tag = f"({cat}, {dlg})" if dlg != "self" else f"({cat})"
        lines.append(f"- [{icon.get(t['status'], '○')}] {tag} {t['content']} ({t['priority']})")
    lines.append("\nUpdate todos via the todowrite tool. Pass the COMPLETE list each time.")
    lines.append("Your todo list is tracked by the continuation system. "
                 "Incomplete todos will trigger automatic continuation.")
    return "\n".join(lines)

# 在 build_system_prompt() 末尾追加：
def build_system_prompt(session_id: str, ...):
    parts = [skill_block, persona_block, memory_block]
    todo_block = _build_todo_block(session_id)  # ← 新增
    if todo_block:
        parts.append(todo_block)
    return "\n\n".join(parts)
```

### 为什么这比 oh-my-openagent-dev 的 5 层防御更轻量

| oh-my-openagent-dev 的 5 层   | sherry_agent 的 1 层                                |
| ----------------------------- | --------------------------------------------------- |
| 1. Prune 保护列表             | 不需要（todos 在 system prompt 中，不在对话历史中） |
| 2. 压缩前快照 + 压缩后恢复    | 不需要（system prompt 每次重建时从 DB 实时读取）    |
| 3. 8 段式压缩上下文注入       | 不需要（todos 不在对话历史中，压缩不影响）          |
| 4. 60s 压缩保护窗口           | 不需要（无续作注入器需要保护）                      |
| 5. 续作强制器（含 todo 列表） | 不需要（todos 始终在 system prompt 中）             |
| **总计 ~600+ 行代码**         | **~20 行代码**                                      |

### 原理

opencode 的 todo 状态存在**对话历史**中（工具返回值），压缩就丢失。sherry_agent 把 todo 状态存在**系统提示词**中（从 DB 实时读取），而系统提示词永远不被压缩，压缩后还会重建。

### token 成本

每次请求额外 ~50-200 tokens（取决于 todo 数量）。可接受的代价。

### session_id 来源：复用 _WORKSPACE_STATE_KEY 缓存

`build_system_prompt()` 已接收 `session_id` 参数，且与 `_WORKSPACE_STATE_KEY` 缓存机制配合使用。todo block 的注入不会破坏现有缓存逻辑——`_build_todo_block()` 从 DB 同步读取，不依赖缓存，每次构建时实时注入。

```python
# 现有调用链（无需改动）：
# ContextEngineHook.wrap_model_call
#   → build_system_prompt(session_id=session_id, ...)  # session_id 已在参数中
#     → _build_todo_block(session_id)  # ← 新增，复用同一个 session_id
```

缓存行为：

- `_WORKSPACE_STATE_KEY` 缓存的是人格静态文件块（per-session 冻结），todo block 不参与缓存
- todo block 每次从 DB 实时读取，反映最新状态
- 压缩后 `build_system_prompt()` 被重新调用时，todo block 自动获取最新 todos

---

## Layer 5: 后端 WS 推送层

已在 Layer 2 的 `service.update_todos()` 中实现（`_push_todo_update`）。

不需要新建 WS 端点——复用通用 WS（`/sessions/ws`），通过 `relation_register.get_websocket_by_session_id()` 直发。前端 `ws.ts` 已有 `onmessage` -> `emit` 分发机制。

### 消息格式

```json
{
  "event": "todo_updated",
  "session_id": "xxx",
  "content": {
    "todos": [
      { "content": "实现登录", "status": "completed", "priority": "high" },
      { "content": "写测试", "status": "in_progress", "priority": "medium" }
    ]
  }
}
```

### 推送路径

```
工具 execute (session_id 可用)
  → service.update_todos()
  → _push_todo_update(session_id, todos)
  → relation_register.get_websocket_by_session_id(session_id)
  → websocket.send_text({event: "todo_updated", ...})
  → 前端 ws.ts onmessage
  → mitt.emit('ws:todo_updated', data)
  → useTodoList.ts 监听更新
```

### 参考实现

- `server/service/heartbeat.py` 的 `push_heartbeat_updated()`
- `skills/builtin/core/cron/scripts/base.py` 的 `_push_cron_notification()`
- `agent/tools/subagent/events/bridge.py` 的 `_deliver_to_websocket()`

---

## Layer 6: 前端状态层

- **新建文件**: `client/app/composables/useTodoList.ts`

复用 `useSubagentTasks.ts` 的模块级单例模式。

### 实现

```typescript
import { ref, computed } from "vue";
import { on, emit } from "~/composables/mitt";
import { useUiStore } from "~/stores/ui";

interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  category?: "quick" | "deep" | "ultrabrain" | "visual" | "git" | "writing";
  delegation?: "self" | "subagent";
  subagent_id?: string | null;
}

const todos = ref<Todo[]>([]);
let subscribed = false;

function setupListeners(): void {
  if (subscribed) return;
  subscribed = true;

  // WS 事件监听
  on("ws:todo_updated", (payload: any) => {
    todos.value = payload?.content?.todos ?? [];
  });

  // WS 重连后，发消息让后端重发 todos
  on("ws:reconnected", () => {
    emit("ws:send", {
      type: "todo_refresh",
      session_id: currentSessionId.value,
    });
  });
}

// 状态机
const todoState = computed<"hide" | "open" | "close">(() => {
  if (todos.value.length === 0) return "hide";
  const allDone = todos.value.every(
    (t) => t.status === "completed" || t.status === "cancelled",
  );
  return allDone ? "close" : "open";
});

const dockVisible = computed(() => todoState.value === "open");
const doneCount = computed(
  () =>
    todos.value.filter(
      (t) => t.status === "completed" || t.status === "cancelled",
    ).length,
);

export function useTodoList() {
  const uiStore = useUiStore(); // collapsed 从 Pinia 获取
  setupListeners();
  return {
    todos,
    collapsed: computed(() => uiStore.todoDockCollapsed),
    toggleCollapsed: () => uiStore.toggleTodoDock(),
    dockVisible,
    doneCount,
  };
}
```

### 简化点（相比 opencode）

- 三态 `hide/open/close`，去掉 `clear`（无 `session_working` 等价物）
- 去掉延迟关闭动画（直接 `close`）
- `collapsed` 放入 Pinia `ui.ts` store，持久化到 localStorage（`persist: { pick: ['todoDockCollapsed'] }`）
- 不需要 HTTP REST API，WS 重连后无需拉取（等 LLM 下次 todowrite 自然推送）

### 参考文件

- `client/app/composables/useSubagentTasks.ts`（模块级单例模式，799 行）
- `client/app/composables/mitt.ts`（mitt 事件总线，13 行极简）
- `client/app/composables/ws.ts`（WS 单例 + mitt 分发，398 行）

### Pinia ui.ts store 扩展

- **修改文件**: `client/app/stores/ui.ts`

```typescript
export const useUiStore = defineStore(
  "ui",
  () => {
    // ... 现有状态 ...
    const todoDockCollapsed = ref(false); // ← 新增

    const toggleTodoDock = () => {
      // ← 新增
      todoDockCollapsed.value = !todoDockCollapsed.value;
    };

    return {
      // ... 现有导出 ...
      todoDockCollapsed,
      toggleTodoDock,
    };
  },
  {
    persist: { pick: ["sidebarCollapsed", "todoDockCollapsed"] }, // ← 新增 todoDockCollapsed
  },
);
```

---

## Layer 7: 前端 WS 事件分发

- **修改文件**: `client/app/composables/ws.ts`

```typescript
// 在 onmessage handler 中新增：
socket.onmessage = (event) => {
  const data = JSON.parse(event.data);
  const eventType = data.event ?? "";

  // ... 现有事件处理 ...

  if (eventType === "todo_updated") {
    emit("ws:todo_updated", data); // ← 新增
  }

  emit("ws:message", data); // 透传
};
```

---

## Layer 8: UI 组件层

### TodoDock.vue

- **新建文件**: `client/app/components/chat/TodoDock.vue`

```vue
<template>
  <Transition name="dock-slide">
    <div v-if="dockVisible" class="todo-dock">
      <!-- 头部：进度 + 折叠按钮 -->
      <div class="dock-header flex items-center justify-between px-3 py-2">
        <span class="text-sm text-color-secondary">
          {{ t("todolist.progress", { done: doneCount, total: todos.length }) }}
        </span>
        <Button text rounded size="small" @click="collapsed = !collapsed">
          <i :class="['pi', collapsed ? 'pi-chevron-down' : 'pi-chevron-up']" />
        </Button>
      </div>

      <!-- 列表 -->
      <div v-show="!collapsed" class="todo-list overflow-y-auto max-h-42">
        <TodoItem v-for="(todo, i) in todos" :key="i" :todo="todo" />
      </div>
    </div>
  </Transition>
</template>
```

### TodoItem.vue

- **新建文件**: `client/app/components/chat/TodoItem.vue`

```vue
<template>
  <div class="todo-item flex items-center gap-2 px-3 py-1">
    <Checkbox
      :model-value="todo.status === 'completed'"
      :indeterminate="todo.status === 'in_progress'"
      :readonly="true"
    />
    <!-- E6: category badge -->
    <span
      v-if="todo.category && todo.category !== 'quick'"
      class="text-xs px-1.5 py-0.5 rounded bg-surface-200 text-color-secondary"
    >
      {{ todo.category }}
    </span>
    <span
      :class="[
        'text-sm',
        todo.status === 'completed' || todo.status === 'cancelled'
          ? 'line-through text-color-secondary'
          : 'text-color',
      ]"
    >
      {{ todo.content }}
    </span>
    <!-- E6: delegation indicator -->
    <i
      v-if="todo.delegation === 'subagent'"
      class="pi pi-external-link text-xs text-color-secondary"
      v-tooltip="'Delegated to subagent'"
    />
    <span
      v-if="todo.status === 'in_progress'"
      class="pulse-dot w-2 h-2 rounded-full bg-primary animate-pulse"
    />
  </div>
</template>
```

### 组件映射

| opencode 组件        | sherry_agent 替代                                      |
| -------------------- | ------------------------------------------------------ |
| Checkbox (kobalte)   | PrimeVue `<Checkbox>` (`:indeterminate`, `:readonly`)  |
| AnimatedNumber       | 纯文本 `{{ doneCount }}/{{ todos.length }}`            |
| TextStrikethrough    | CSS `line-through`                                     |
| useSpring            | Vue `<Transition>` + CSS `transition: max-height 0.3s` |
| IconButton (chevron) | PrimeVue `<Button text>` + `pi pi-chevron-down`        |
| DockTray             | 普通 `<div>` + TailwindCSS                             |
| Category badge (E6)  | `<span class="text-xs px-1.5 rounded bg-surface-200">` |
| Delegation icon (E6) | `<i class="pi pi-external-link">` + PrimeVue Tooltip   |

### 布局集成

- **修改文件**: `client/app/pages/home/index/[sid].vue`

在 `<div class="relative">` 内部、`<div class="flex flex-col h-40">` 之前插入：

```vue
<div class="relative">
  <!-- [6] WS 重连横幅 -->
  <TodoDock v-if="sessionId" />          <!-- ← 新增 -->
  <div class="flex flex-col h-40">
    <!-- [7] 工具栏 + ChatInputBox -->
  </div>
</div>
```

---

## Layer 9: i18n

- **修改文件**: `client/app/i18n/locales/{en,ja,ko,zh}.json`

### zh.json

```json
{
  "todolist": {
    "progress": "已完成 {done} 个任务（共 {total} 个）",
    "collapse": "折叠",
    "expand": "展开"
  }
}
```

### en.json

```json
{
  "todolist": {
    "progress": "{done} of {total} tasks completed",
    "collapse": "Collapse",
    "expand": "Expand"
  }
}
```

### i18n 目录结构

```
client/app/i18n/locales/
├── en.json
├── ja.json
├── ko.json
└── zh.json
```

使用方式: `const { t } = useI18n(); t('todolist.progress', { done: 3, total: 5 })`

---

## 完整文件清单

| #   | 操作 | 文件路径                                         | 参考来源                                                            |
| --- | ---- | ------------------------------------------------ | ------------------------------------------------------------------- |
| 1   | 新建 | `agent/tools/todolist/__init__.py`               | taskflow/**init**.py                                                |
| 2   | 新建 | `agent/tools/todolist/config.py`                 | taskflow/config.py（简化）                                          |
| 3   | 新建 | `agent/tools/todolist/registry/store_sqlite.py`  | taskflow/registry/store_sqlite.py（去乐观锁）+ E6 schema            |
| 4   | 新建 | `agent/tools/todolist/service.py`                | 新设计（service + WS 推送）+ E6 校验/屏障                           |
| 5   | 新建 | `agent/tools/todolist/tools/__init__.py`         | taskflow/tools/**init**.py + E2 格式规则 + E6 字段描述              |
| 6   | 新建 | `agent/tools/todolist/tools/todowrite.py`        | taskflow/tools/taskflow_create.py + E6a fan-out                     |
| 7   | 新建 | `agent/tools/todolist/tools/todoread.py`         | 新设计（opencode 没有，改进）                                       |
| 8   | 修改 | `agent/tools/__init__.py`                        | 加入 build_todolist_tools                                           |
| 9   | 新建 | `skills/todolist/SKILL.md`                       | skills/taskflow/SKILL.md                                            |
| 10  | 修改 | `workspace/prompt_builder.py`                    | 新增 _build_todo_block() ★ + E1 hook 告知 + E6 显示                 |
| 11  | 修改 | `workspace/template/en/AGENTS.md`                | E1: Task Management MANDATORY + E6c 委派指令 + E6d 转换屏障         |
| 12  | 修改 | `workspace/template/zh/AGENTS.md`                | E1: Task Management MANDATORY + E6c 委派指令 + E6d 转换屏障（中文） |
| 13  | 新建 | `agent/tools/todolist/stagnation_tracker.py`     | E3: 停滞检测 + 退避冷却                                             |
| 14  | 新建 | `agent/middlewares/todo_continuation.py`         | E3: after_agent 续作中间件                                          |
| 15  | 修改 | `agent/core.py`                                  | E3: 注册 TodoContinuationEnforcer 中间件                            |
| 16  | 修改 | `client/app/stores/ui.ts`                        | 新增 todoDockCollapsed + persist                                    |
| 17  | 新建 | `client/app/composables/useTodoList.ts`          | useSubagentTasks.ts 模式                                            |
| 18  | 修改 | `client/app/composables/ws.ts`                   | 新增 todo_updated 事件分发 + ws:send 发送能力                       |
| 19  | 修改 | `server/trigger/core.py`                         | 新增 todo_refresh 消息处理                                          |
| 20  | 新建 | `client/app/components/chat/TodoDock.vue`        | 新设计（PrimeVue）                                                  |
| 21  | 新建 | `client/app/components/chat/TodoItem.vue`        | 新设计（PrimeVue）+ E6 category/delegation 显示                     |
| 22  | 修改 | `client/app/pages/home/index/[sid].vue`          | 插入 TodoDock                                                       |
| 23  | 修改 | `client/app/i18n/locales/en.json`                | 新增 todolist 域                                                    |
| 24  | 修改 | `client/app/i18n/locales/zh.json`                | 新增 todolist 域                                                    |
| 25  | 修改 | `client/app/i18n/locales/ja.json`                | 新增 todolist 域                                                    |
| 26  | 修改 | `client/app/i18n/locales/ko.json`                | 新增 todolist 域                                                    |
| 27  | 修改 | `agent/middlewares/subagent_completion_drain.py` | E5: 子代理完成后追加验证提醒（Phase 5 可选）                        |

> **注**: #11-15, #27 为强制层文件，#3-6, #10-12, #21 含 E6 委派字段扩展，实现细节详见 [TODOLIST_ENFORCEMENT.md](./TODOLIST_ENFORCEMENT.md)

---

## 实现顺序

```
Phase 1: 后端数据层 + 工具层 + 压缩保护 + 强制语言 (Layer 1-4 + E1 + E2 + E6a/b/c/d-prompt)
  1. store_sqlite.py — DB 表 + CRUD + E6 schema (category/delegation/subagent_id)
  2. service.py — update_todos + get_todos + WS 推送 + E6 category/delegation 校验
  3. config.py — 常量
  4. todowrite.py + todoread.py — @tool 定义（含 E2 工具描述强制语言 + E6a fan-out reminder）
  5. tools/__init__.py — build_todolist_tools（含 E6b category/delegation 字段描述）
  6. agent/tools/__init__.py — 注册
  7. SKILL.md
  8. prompt_builder.py — _build_todo_block 注入（含 E6 category/delegation 显示）
  9. AGENTS.md 模板 — 添加 E1 系统提示词强制语言 + E6c 委派指令 + E6d-prompt 转换屏障

Phase 2: Continuation Enforcer (E3) + 转换屏障代码级 (E6d-code) — 详见 TODOLIST_ENFORCEMENT.md
  10. stagnation_tracker.py — 停滞检测 + 退避
  11. todo_continuation.py — after_agent 中间件
  12. agent/core.py — 注册中间件
  13. service.py — E6d-code: subagent_id 运行检查（复用 subagent registry API）

Phase 3: 前端通信层 (Layer 5-6)
  14. ws.ts — todo_updated 事件分发 + ws:send 发送能力
  15. server/trigger/core.py — todo_refresh 消息处理
  16. useTodoList.ts — 模块级单例 + WS 订阅 + 重连重发

Phase 4: 前端 UI 层 (Layer 7-8)
  17. TodoItem.vue + TodoDock.vue — PrimeVue 组件（含 E6 category/delegation 显示）
  18. [sid].vue — 布局集成
  19. i18n — 4 语言翻译

Phase 5: 验证提醒 (E5，可选) — 详见 TODOLIST_ENFORCEMENT.md
  20. 扩展 SubagentCompletionDrainMiddleware — 子代理完成后追加验证提醒
```

---

## 关键设计决策

| 决策          | 选择                                                              | 理由                                                     |
| ------------- | ----------------------------------------------------------------- | -------------------------------------------------------- |
| 数据存储      | 独立 DB `todos.db`                                                | 隔离，不污染其他 DB                                      |
| 工具数量      | 2 个（todowrite + todoread）                                      | opencode 只有 1 个是缺陷，todoread 是廉价安全网          |
| 更新策略      | 全量替换                                                          | 简单，单写者无并发                                       |
| 压缩保护      | 系统提示词注入                                                    | 利用 sherry 已有的 prompt 重建机制，~20 行代码解决       |
| WS 推送       | relation_register 直发                                            | 复用 heartbeat/cron 已验证模式                           |
| 状态机        | 3 态（hide/open/close）                                           | 比 opencode 4 态更简单，无 session_working               |
| 动画          | CSS transition                                                    | 比 motion 库简单，够用                                   |
| 前端状态      | 模块级单例 ref + Pinia store                                      | todos 用模块级单例，collapsed 用 Pinia 持久化            |
| 前端组件库    | PrimeVue + TailwindCSS                                            | sherry_agent 现有 UI 栈                                  |
| 强制策略      | 5 层软强制（非硬阻塞）                                            | 提示词威慑 + 后置续作 + 多点提醒，不阻塞工具             |
| Subagent 联动 | E6 四机制（#1 fan-out + #2 category + #3 委派指令 + #4 转换屏障） | omo 全 prompt 级方案，适配 sherry 复用 subagent registry |
| Todo schema   | 3 扩展字段（category/delegation/subagent_id）                     | 支持委派路由和转换屏障，向后兼容（有默认值）             |

---

## 待确认事项（后续根据实际情况调整）

~~1. **HTTP REST API**: 是否需要 `GET /sessions/:id/todos` 端点？~~ → 已决定：不需要，用 WS 重连重发
~~2. **`prompt_builder.py` 的 `session_id` 来源**: 当前 `build_system_prompt()` 是否已接收 `session_id` 参数？~~ → 已确认：已有 session_id 参数，复用 _WORKSPACE_STATE_KEY 缓存
~~3. **`collapsed` 状态持久化**: 用 `localStorage` 直接存还是放 `ui.ts` Pinia store？~~ → 已决定：放 Pinia ui.ts store

---

## 强制执行层（Enforcement Layers）

详见 **[TODOLIST_ENFORCEMENT.md](./TODOLIST_ENFORCEMENT.md)** — 包含 E1（系统提示词）、E2（工具描述）、E3（续作强制器）、E5（验证提醒）、E6（委派与 Fan-out）的完整实现细节、适配性评估、不适合的 omo 机制。

---

## 三项目对比参考

### opencode TodoList

- 1 个工具 (todowrite)，全量替换，无 todoread
- todo 状态存在对话历史中（工具返回值），压缩后丢失
- 无任何压缩保护机制
- SolidJS + motion + kobalte UI
- SSE 事件推送
- 6 个自定义 UI 组件

### oh-my-openagent-dev 修复方案

- 5 层防御：Prune 保护 + 快照/恢复 + 8 段式上下文注入 + 60s 保护窗口 + 续作强制器
- ~600+ 行修复代码
- 将 todo 状态提升为独立持久化存储
- capture -> restore -> bootstrap 拦截三步

### sherry_agent 方案（本方案）

- 2 个工具 (todowrite + todoread)
- 系统提示词注入（~20 行代码，天然免疫压缩）
- todo 状态存在 DB + 系统提示词中（不在对话历史中）
- Vue 3 + PrimeVue + TailwindCSS
- WebSocket 推送（relation_register 直发）
- 3 态简化状态机
- E1+E2 强制语言（系统提示词 + 工具描述，Phase 1）
- E3 续作强制器（after_agent 中间件 + auto_turn 复用，Phase 2）
- E5 验证提醒（子代理完成后，Phase 5 可选）
- E6 委派与 Fan-out（#1 fan-out reminder + #2 category 路由 + #3 委派指令 + #4 转换屏障，Phase 1+2）
