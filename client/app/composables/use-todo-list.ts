/**
 * Session plan (todo list) state composable — module-level singleton.
 *
 * Mirrors the `useSubagentTasks.ts` singleton pattern: state and the mitt
 * subscription live outside the exported function, so every consumer (the
 * `TodoDock` component, the session page) shares one reactive source of truth
 * and the WS listeners are registered exactly once.
 *
 * Data flow:
 * - The backend pushes `{"event":"todo_updated","session_id":...,"content":{"todos":[...]}}`
 *   on every `TodoService.update_todos` (see `agent/tools/todolist/service.py`).
 *   The long-lived `/sessions/ws` singleton forwards those frames as the
 *   `ws:todo_updated` mitt event; the agent stream socket forwards the same
 *   frames while a turn is running (see `bridge/ws-stream.ts`).
 * - On reconnect the composable asks the backend to re-send the persisted list
 *   by emitting a `ws:send` frame with `event: "todo_refresh"`; the
 *   `server/trigger/core.py` handler answers with a fresh `todo_updated`.
 *
 * @module useTodoList
 */

import { computed, ref, type ComputedRef, type Ref } from 'vue';

/** Todo status vocabulary (mirrors the backend store). */
export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';

/** Todo priority vocabulary (mirrors the backend store). */
export type TodoPriority = 'high' | 'medium' | 'low';

/** Optional routing/quality category (E6). */
export type TodoCategory = 'quick' | 'deep' | 'ultrabrain' | 'visual' | 'git' | 'writing';

/** Whether the work is done inline or delegated to a subagent. */
export type TodoDelegation = 'self' | 'subagent';

/**
 * One session todo row. `flow_id` / `step_id` link the todo to a TaskFlow
 * flow/step; the frontend never recomputes DAG state, it only displays the
 * association (TaskFlow owns scheduling).
 */
export interface Todo {
  content: string;
  status: TodoStatus;
  priority: TodoPriority;
  category?: TodoCategory;
  delegation?: TodoDelegation;
  subagent_id?: string | null;
  /** TaskFlow flow id this todo tracks (null/absent = unlinked plan todo). */
  flow_id?: string | null;
  /** TaskFlow step id (e.g. `step-2`). */
  step_id?: string | null;
  /** TaskFlow step status read-back (never computed here). */
  taskflow_status?: 'blocked' | 'ready' | 'dispatched' | 'done' | null;
  /** Plan file reference (`.omo/plans/*.md`). */
  plan_ref?: string | null;
}

/** A display group: all todos sharing one TaskFlow flow, or the unlinked plan todos. */
export interface TodoGroup {
  /** TaskFlow flow id, or `null` for the trailing unlinked group. */
  flowId: string | null;
  items: Todo[];
}

/** Tri-state dock visibility derived from the list contents. */
export type TodoState = 'hide' | 'close' | 'open';

/* ---------------------------------------------------------------------------
 * Module singleton state
 * ------------------------------------------------------------------------- */

/** Current session's todos (replaced wholesale on every `todo_updated` frame). */
const todos = ref<Todo[]>([]);

/** Session id used for refresh/init; updated by `init(sid)`. */
const currentSid = ref('');

/** Singleton guard: listeners are registered exactly once per process. */
let subscribed = false;

/**
 * Resolve the active session id from the URL path (module-level safe).
 * @returns The trailing path segment, or `''` when it is not a session.
 */
function resolveSid(): string {
  if (typeof window === 'undefined') return '';
  const segs = window.location.pathname.split('/').filter(Boolean);
  const last = segs[segs.length - 1];
  return last && last !== 'home' && last !== 'tasks' ? last : '';
}

/** Register the mitt listeners once (singleton guard). */
function setupListeners(): void {
  if (subscribed) return;
  subscribed = true;

  on('ws:todo_updated', (payload: unknown) => {
    const frame = payload as { content?: { todos?: unknown } } | null | undefined;
    const list = frame?.content?.todos;
    todos.value = Array.isArray(list) ? (list as Todo[]) : [];
  });

  // The backend re-sends nothing proactively after a reconnect; ask for a snapshot.
  on('ws:reconnected', () => {
    refresh();
  });
}

/**
 * Ask the backend to re-send the persisted todo list for the current session.
 * Emits the outbound `ws:send` frame handled by `ws.ts`.
 * @param sid Optional explicit session id (defaults to the last known/URL sid).
 */
function refresh(sid?: string): void {
  const target = sid ?? currentSid.value ?? resolveSid();
  currentSid.value = target ?? '';
  emit('ws:send', { event: 'todo_refresh', session_id: currentSid.value, content: '' });
}

/**
 * Initialise the singleton for a session: install the listeners once and pull
 * the current snapshot. Idempotent; safe to call from every consumer mount.
 * @param sid Current session id (route param).
 */
function init(sid?: string): void {
  setupListeners();
  if (sid) currentSid.value = sid;
  if (currentSid.value) refresh(currentSid.value);
}

/* ---------------------------------------------------------------------------
 * Derived state
 * ------------------------------------------------------------------------- */

/**
 * True for terminal todos (completed or cancelled).
 * @param todo Candidate todo.
 * @returns True when the todo reached a terminal status.
 */
function isTerminal(todo: Todo): boolean {
  return todo.status === 'completed' || todo.status === 'cancelled';
}

/**
 * Tri-state dock visibility:
 * - `hide`  — no todos at all;
 * - `close` — every todo reached a terminal status (nothing left to do);
 * - `open`  — at least one todo is still pending/in_progress.
 */
const todoState: ComputedRef<TodoState> = computed(() => {
  if (todos.value.length === 0) return 'hide';
  return todos.value.every(isTerminal) ? 'close' : 'open';
});

/** Whether the dock should be rendered (only while work remains). */
const dockVisible: ComputedRef<boolean> = computed(() => todoState.value === 'open');

/** Number of terminal todos. */
const doneCount: ComputedRef<number> = computed(() => todos.value.filter(isTerminal).length);

/**
 * Group todos per TaskFlow flow, preserving first-seen order: all linked flows
 * first (in the order their first todo appeared), then the unlinked plan todos
 * as the trailing group. Item order within a group is left exactly as received
 * (stable) — DAG ordering is TaskFlow's job, not the frontend's.
 */
const groups: ComputedRef<TodoGroup[]> = computed(() => {
  const linked: TodoGroup[] = [];
  const byFlow = new Map<string, TodoGroup>();
  const unlinked: Todo[] = [];

  for (const todo of todos.value) {
    const flowId = todo.flow_id;
    if (flowId) {
      let group = byFlow.get(flowId);
      if (!group) {
        group = { flowId, items: [] };
        byFlow.set(flowId, group);
        linked.push(group);
      }
      group.items.push(todo);
    } else {
      unlinked.push(todo);
    }
  }

  return unlinked.length > 0 ? [...linked, { flowId: null, items: unlinked }] : linked;
});

/**
 * Create the plan-dock API. Every consumer shares the same module-level state;
 * only the Pinia-backed collapse flag is resolved per call.
 * @returns The shared plan-dock state, derived views and actions.
 */
export function useTodoList() {
  const uiStore = useUiStore();
  setupListeners();

  return {
    // State
    todos: todos as Ref<Todo[]>,
    // Derived
    todoState,
    dockVisible,
    doneCount,
    groups,
    // Collapse (persisted via the UI store)
    collapsed: computed(() => uiStore.todoDockCollapsed as boolean),
    toggleCollapsed: () => uiStore.toggleTodoDock(),
    // Behavior
    refresh,
    init
  };
}
