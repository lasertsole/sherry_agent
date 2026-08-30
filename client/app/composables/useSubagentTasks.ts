/**
 * Shared "background tasks" state composable (module-level singleton)
 *
 * Responsibility: centrally manages all state plus the loading/WS/Dexie caching
 * logic for subagent run records (taskRuns), so that the left sidebar (SessionSidebar.vue)
 * and the right full task list view (SubagentTasksView.vue) share the same reactive
 * data and stay consistent in real time without duplicate subscriptions.
 *
 * Uses a module-level singleton (state declared outside the setup composable) instead of
 * instance-level state: no matter how many components call it, they all get the same
 * taskRuns / taskLoading / …, and the WS subscription is established only once.
 */
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { on } from './mitt';
import { fetchSubagentRuns, fetchSubagentRunSubtree, deleteSubagentRunSubtree, type SubagentRun } from './bridge';
import { cacheSubagentRuns, readCachedSubagentRuns, deleteCachedSubagentRuns, type CachedSubagentRun } from './db';
import { useSubagentWs } from './ws';

/* ------------------------------------------------------------------ */
/* Module-level singleton state (shared single source of truth)                           */
/* ------------------------------------------------------------------ */
/** Subagent run record list (filtered to the current session; used by the chat page jump bar / sidebar red dot to detect this session's tasks) */
const taskRuns = ref<SubagentRun[]>([]);
/** Global subagent run record list (all sessions included; used by the background tasks view to show tasks from every session) */
const allTaskRuns = ref<SubagentRun[]>([]);
/** Background tasks currently loading */
const taskLoading = ref(false);
/** Timestamp of the last successful fetch (ms) */
const lastTasksFetchedAt = ref<number>(0);
/** Whether a "background tasks" realtime message has ever been received (avoids duplicate full fetches on first paint / reconnect) */
const subagentWsReady = ref(false);
/** Last initialized session (used to refresh the list when switching sessions) */
let lastLoadedSessionId: string | undefined;

/** Set of root run_ids currently selected in the background tasks tab (for multi-select / select-all / batch delete) */
const selectedRunIds = ref<Set<string>>(new Set());
/** Batch deletion currently in progress on the background tasks tab */
const deletingRunIds = ref<Set<string>>(new Set());

/** Whether the "background tasks" view is currently shown (true while the sidebar tasks tab or the right-side task view is active) */
const tasksTabActive = ref(false);

/* Session key prefixes: the backend uses prefixed keys when attributing subtasks to the calling session; display/Nav needs them normalized to bare UUIDs */
const SESSION_KEY_PREFIXES = ['agent:main:session:', 'agent:subagent:'];

/** Normalize a session key: strip the prefix to get the bare UUID; keys without a prefix (already bare/'/'-'/default) are returned as-is. */
function normalizeSessionKey(key: string | null | undefined): string | null {
  if (!key) return null;
  for (const prefix of SESSION_KEY_PREFIXES) {
    if (key.startsWith(prefix)) {
      const bare = key.slice(prefix.length);
      return bare || null;
    }
  }
  return key;
}

/** Set of bare UUIDs of sessions that "still exist".
 *  Data source: the authoritative server `/sessions` list + local Dexie session placeholders; populated by loadSubagentValidSessions().
 *  Purpose: lets SubagentTasksView verify that a run's "back to session" target really exists — if not, that button is hidden. */
const subagentValidSessionIds = ref<Set<string>>(new Set());
/** Whether it has already been loaded (avoids re-fetching the session list on every session switch). */
let subagentSessionsLoaded = false;

/**
 * Fetch and cache the set of sessions that "still exist".
 * Used by SubagentTasksView for "back to session" existence checks: for an orphaned run
 * whose normalized requester_session_key is not in this set, the task box is still shown
 * as usual, but the "back to session" button is hidden.
 * Idempotent: it only actually fetches once (unless cross-window state was cleared;
 * can be explicitly called again).
 */
async function loadSubagentValidSessions(): Promise<void> {
  if (subagentSessionsLoaded) return;
  const { getSessionList } = await import('@/composables/messages');
  const { readCachedSessionMetaList } = await import('@/composables/db');
  try {
    const sessions = await getSessionList();
    const placeholders = await readCachedSessionMetaList();
    const set = new Set<string>();
    for (const s of sessions) if (s.id) set.add(s.id);
    for (const p of placeholders) if (p.id) set.add(p.id);
    subagentValidSessionIds.value = set;
    subagentSessionsLoaded = true;
  } catch (error) {
    // A fetch failure must not block the UI: it is equivalent to "unable to confirm the target session", so the button is simply hidden as if there were no valid target.
    console.warn('[useSubagentTasks] 拉取会话列表失败，无法校验「返回会话」目标：', error);
  }
}

/* ------------------------------------------------------------------ */
/* Task view expand/flow-graph state (hoisted into the singleton, kept alive across chat↔tasks switches)        */
/* ------------------------------------------------------------------ */

/** Currently expanded run_id (clicking a task card header toggles expand/collapse); preserved across chat↔tasks switches */
const expandedRunId = ref<string | undefined>(undefined);
/** Selected run of the embedded flow graph (passed through to SubagentFlowGraph's defineModel selected-run-id) */
const selectedRunId = ref<string | undefined>(undefined);
/** run_id currently focused in the right-side task view (set when clicking a background task Box on the left; used to display that run's subtree) */
const focusedRunId = ref<string | undefined>(undefined);

/** Click a task card header: expand/collapse the given run and sync the selected state. */
function toggleExpandRun(runId: string): void {
  if (expandedRunId.value === runId) {
    expandedRunId.value = undefined;
    selectedRunId.value = undefined;
  } else {
    expandedRunId.value = runId;
    selectedRunId.value = runId;
  }
}

/** Focus/expand the given run (called when clicking a sidebar task item); if found, expand it and sync the selected state. */
function focusRun(runId: string | undefined): void {
  if (!runId) return;
  expandedRunId.value = runId;
  selectedRunId.value = runId;
  // Also record the run focused by the right-side view, used to display that run's subtree
  focusedRunId.value = runId;
}

/** When switching back from the task view to chat, the expanded state is kept rather than reset (call explicitly to clear it). */
function resetFlowState(): void {
  expandedRunId.value = undefined;
  selectedRunId.value = undefined;
}

/** Whether the WS subscription has been established (singleton guard, avoids duplicate on() subscriptions) */
let subscribed = false;

/* ------------------------------------------------------------------ */
/* Internal helper functions                                                         */
/* ------------------------------------------------------------------ */

/** Whether the run is still running (RUNNING / INTERRUPTED count as not yet finished) */
function isRunning(run: SubagentRun): boolean {
  const status = run?.execution?.status;
  return status === 'RUNNING' || status === 'INTERRUPTED';
}

/**
 * Normalize a backend-shaped SubagentRun into the Dexie cache shape CachedSubagentRun.
 * The two shapes share the same field names and differ only in nullability / optional
 * nesting; this does a one-time fallback pass so undefined never gets written into the cache.
 */
function toCachedSubagentRun(run: SubagentRun): CachedSubagentRun {
  return {
    run_id: run.run_id,
    child_session_key: run.child_session_key ?? null,
    requester_session_key: run.requester_session_key ?? null,
    task: run.task ?? null,
    task_name: run.task_name ?? null,
    label: run.label ?? null,
    spawn_mode: run.spawn_mode ?? null,
    context_mode: run.context_mode ?? null,
    agent_id: run.agent_id ?? null,
    depth: run.depth ?? null,
    role: run.role ?? null,
    control_scope: run.control_scope ?? null,
    generation: run.generation ?? null,
    swarm_group_id: run.swarm_group_id ?? null,
    swarm_run_state: run.swarm_run_state ?? null,
    ended_reason: run.ended_reason ?? null,
    pause_reason: run.pause_reason ?? null,
    execution: run.execution
      ? {
          status: run.execution.status ?? null,
          outcome: run.execution.outcome?.status ?? null,
          started_at: run.execution.started_at != null ? String(run.execution.started_at) : null,
          completed_at: run.execution.ended_at != null ? String(run.execution.ended_at) : null
        }
      : null,
    completion: run.completion
      ? {
          required: run.completion.required ?? null,
          owner_session_key: null,
          result_text: run.completion.result_text ?? null,
          captured_at: run.completion.captured_at != null ? String(run.completion.captured_at) : null
        }
      : null,
    delivery: run.delivery
      ? {
          status: run.delivery.status ?? null,
          attempt_count: run.delivery.attempt_count ?? null,
          delivered_at: run.delivery.delivered_at != null ? String(run.delivery.delivered_at) : null
        }
      : null
  };
}

/** Restore a Dexie cache-shaped record back into the UI display shape SubagentRun. */
function toSubagentRun(c: CachedSubagentRun): SubagentRun {
  return {
    run_id: c.run_id,
    task_run_id: null,
    child_session_key: c.child_session_key ?? '',
    requester_session_key: c.requester_session_key ?? '',
    task: c.task ?? '',
    task_name: c.task_name ?? undefined,
    label: c.label ?? undefined,
    spawn_mode: c.spawn_mode ?? undefined,
    context_mode: c.context_mode ?? undefined,
    agent_id: c.agent_id ?? undefined,
    depth: c.depth ?? undefined,
    role: c.role ?? undefined,
    control_scope: c.control_scope ?? undefined,
    generation: c.generation ?? undefined,
    swarm_group_id: c.swarm_group_id ?? undefined,
    swarm_run_state: c.swarm_run_state ?? undefined,
    ended_reason: c.ended_reason ?? undefined,
    pause_reason: c.pause_reason ?? undefined,
    execution: {
      status: c.execution?.status ?? 'UNKNOWN',
      started_at: c.execution?.started_at != null ? Number(c.execution.started_at) : null,
      ended_at: c.execution?.completed_at != null ? Number(c.execution.completed_at) : null,
      outcome: c.execution?.outcome ? { status: c.execution.outcome, error: null } : { status: 'PENDING', error: null },
      transcript_target: undefined
    },
    completion: {
      required: c.completion?.required ?? false,
      result_text: c.completion?.result_text ?? null,
      captured_at: c.completion?.captured_at != null ? Number(c.completion.captured_at) : null
    },
    delivery: {
      status: c.delivery?.status ?? 'PENDING',
      payload: undefined,
      attempt_count: c.delivery?.attempt_count ?? 0,
      last_error: undefined,
      last_attempt_at: undefined,
      suspended_at: undefined,
      discard_reason: undefined,
      delivered_at: c.delivery?.delivered_at != null ? Number(c.delivery.delivered_at) : undefined
    }
  };
}

/* ------------------------------------------------------------------ */
/* Load / refresh logic                                                      */
/* ------------------------------------------------------------------ */

/**
 * Resolve the currently active session id.
 * Supports two sources: preferably extracted from the URL pathname (safe at module
 * level, no setup context required), or explicitly passed in by the caller via an
 * argument (recommended, keeps the source consistent with the sidebar/router).
 */
function resolveSid(force?: string): string | undefined {
  if (force) return force;
  if (typeof window === 'undefined') return undefined;
  const segs = window.location.pathname.split('/').filter(Boolean);
  const sid = segs[segs.length - 1];
  return sid && sid !== 'home' ? sid : undefined;
}

/** Keep only the cached runs that belong to the current session (or its subtasks). */
function filterBySession(runs: CachedSubagentRun[], sid: string | undefined): SubagentRun[] {
  if (!sid) return [];
  return runs.filter(c => c.requester_session_key === sid || c.child_session_key === sid).map(toSubagentRun);
}

/** Rebuild the lists from the Dexie cache (local immediate update after WS events / session switches / reconnects). */
async function refreshFromCache(sid?: string): Promise<void> {
  try {
    const cached = await readCachedSubagentRuns();
    // The global cache is cumulative data across "all sessions" (the Dexie table is global); map it directly into the global task view data
    allTaskRuns.value = cached.map(toSubagentRun);
    // Session-filtered view: used by the chat page jump bar / sidebar red dot to detect this session's tasks
    const target = resolveSid(sid);
    taskRuns.value = target ? filterBySession(cached, target) : [];
  } catch {
    // Ignore cache read failures; the next loadTaskRuns acts as the fallback
  }
}

/** Refresh taskRuns: first immediately echo the local cache (works offline), then asynchronously fetch from the backend to fill the gaps. */
async function loadTaskRuns(sid?: string): Promise<void> {
  const target = resolveSid(sid);
  taskLoading.value = true;
  // 1) Local cache first: read IndexedDB and render immediately, so refreshes / first paint never show an empty window
  try {
    const cached = await readCachedSubagentRuns();
    if (target) taskRuns.value = filterBySession(cached, target);
    else taskRuns.value = [];
    // Global view sync: regardless of whether there is a target, the global task list always comes from the full cache
    allTaskRuns.value = cached.map(toSubagentRun);
  } catch (e) {
    console.warn('[useSubagentTasks] 读取本地子任务缓存失败，回退服务端：', e);
  }
  // 2) Server-side gap filling: fetch the whole run tree and write it to Dexie, recovering events missed while the WS was disconnected
  if (target) {
    try {
      const runs = await fetchSubagentRuns(target, 'descendants');
      await cacheSubagentRuns(runs.map(toCachedSubagentRun));
      const cached = await readCachedSubagentRuns();
      taskRuns.value = filterBySession(cached, target);
      // After the fetch the full cache is up to date, so refresh the global task list too
      allTaskRuns.value = cached.map(toSubagentRun);
      lastTasksFetchedAt.value = Date.now();
    } catch (e) {
      // Network failure: keep the Dexie cache as fallback instead of clearing the list, avoiding first-paint flicker
      console.error('[useSubagentTasks] 拉取子 Agent 运行记录失败（以本地缓存兜底）', e);
    }
  }
  taskLoading.value = false;
}

/* ------------------------------------------------------------------ */
/* Realtime subscription (singleton)                                                     */
/* ------------------------------------------------------------------ */

/**
 * Establish the /subagents/ws connection and subscribe to realtime events so the
 * background task list updates incrementally and in real time.
 * The subscribed guard ensures the subscription is established only once, no matter
 * how many components call this.
 */
function setupSubagentWs(): void {
  if (subscribed) return;
  subscribed = true;

  useSubagentWs({
    onReconnect: () => {
      // After a successful reconnect the server re-sends ready; trigger another full gap-fill fetch at that point
      subagentWsReady.value = false;
    }
  });

  on('ws:subagent_spawned', (payload: unknown) => {
    const run = payload as SubagentRun;
    if (!run?.run_id) return;
    void cacheSubagentRuns([toCachedSubagentRun(run)]).then(() => refreshFromCache());
  });

  on('ws:subagent_ended', (payload: unknown) => {
    const run = payload as SubagentRun;
    if (!run?.run_id) return;
    // After the run ends, overwrite with the complete state (including outcome / delivery) for final result display
    void cacheSubagentRuns([toCachedSubagentRun(run)]).then(() => refreshFromCache());
  });

  // ready: the server is ready; trigger one full gap-fill (recovers events missed before the connection was established)
  on('ws:subagents:ready', () => {
    subagentWsReady.value = true;
    // Only trigger the full gap-fill while the "background tasks" view is being shown (fetch on actual viewing, avoids pointless requests)
    if (tasksTabActive.value) void loadTaskRuns();
  });
}

/* ------------------------------------------------------------------ */
/* Exported (public) state                                                             */
/* ------------------------------------------------------------------ */

/** Number of running resident subagents (for the "Sessions" tab red-dot badge; filtered to the current session). */
const runningTaskCount = computed(() => taskRuns.value.filter(run => isRunning(run)).length);

/** Number of running subagents across all sessions (for the "background tasks" tab red-dot badge). */
const allRunningTaskCount = computed(() => allTaskRuns.value.filter(run => isRunning(run)).length);

/** Display list for the background tasks tab: only "first-level direct tasks" (depth === 1, i.e. subtasks spawned directly by each session), across all sessions.
 *  Orphaned runs (stale cache whose calling session has already been destroyed) are **still shown**; their "back to session"
 *  target is validated by SubagentTasksView via wouldExistSession, and the button is hidden when it does not exist (the task box is kept). */
const rootTaskRuns = computed(() => allTaskRuns.value.filter(run => run?.depth === 1));

/** Defines the background-task grouping structure after clustering by calling session: each group holds one calling session_id plus the first-level tasks under that group. */
export interface TaskSessionGroup {
  /** Calling session_id (requester_session_key; empty values fall into the fallback key '-') */
  sessionId: string;
  /** First-level task list spawned under that calling session (keeps the original rootTaskRuns order) */
  runs: SubagentRun[];
}

/**
 * Clustered list for the background tasks tab: groups task boxes by "calling session_id"
 * (the requester_session_key of each root task in rootTaskRuns). Multiple root tasks spawned
 * by the same calling session are placed into the same group, so the left column can show
 * intuitive session-based clusters; groups are stably sorted by sessionId (the empty-value
 * group goes last).
 */
const groupedRootTaskRuns = computed<TaskSessionGroup[]>(() => {
  const groups = new Map<string, SubagentRun[]>();
  for (const run of rootTaskRuns.value) {
    const key = run.requester_session_key || '-';
    const list = groups.get(key);
    if (list) list.push(run);
    else groups.set(key, [run]);
  }
  return Array.from(groups.entries())
    .sort(([a], [b]) => {
      // Empty-value group goes last; the rest sort lexicographically by sessionId
      if (a === '-') return 1;
      if (b === '-') return -1;
      return a < b ? -1 : a > b ? 1 : 0;
    })
    .map(([sessionId, runs]) => ({ sessionId, runs }));
});

/**
 * Display list for the right-side task view: when a background task box is clicked, show
 * **all first-level root tasks** under the "calling session" that box belongs to (the
 * requester_session_key of its topmost depth-1 ancestor task), each expanded with its full
 * descendant subtree. When nothing is focused, falls back to showing all first-level tasks.
 *
 * Parent-child linkage (SubagentRun has no parent_run_id): if a parent run's
 * child_session_key = K, then every run with requester_session_key === K is a direct
 * subtask of it. From this:
 * - Upward: if run.requester_session_key === some parent run's child_session_key, that run
 *   is its ancestor;
 * - Downward: starting from the root, collect descendants level by level along the
 *   child_session_key → requester_session_key chain.
 */
const focusedSubtreeRuns = computed<SubagentRun[]>(() => {
  const rootId = focusedRunId.value;
  if (!rootId) return rootTaskRuns.value;
  const pool = allTaskRuns.value;

  // Pre-build a "direct subtasks" index by requester_session_key for bidirectional (upward/downward) lookup
  const byRequester = new Map<string, SubagentRun[]>();
  for (const run of pool) {
    const key = run.requester_session_key;
    if (!key) continue;
    const list = byRequester.get(key);
    if (list) list.push(run);
    else byRequester.set(key, [run]);
  }
  // Index by child_session_key → the "parent run spawned from it", for tracing ancestors upward
  const parentByChildSession = new Map<string, SubagentRun>();
  for (const run of pool) {
    if (run.child_session_key) parentByChildSession.set(run.child_session_key, run);
  }

  // Starting from the focused run, trace upward to the topmost depth-1 ancestor root task
  const focused = pool.find(r => r.run_id === rootId);
  if (!focused) return [];
  let top: SubagentRun = focused;
  let guard = 0;
  while (top.depth !== 1 && guard++ < 100) {
    const parent: SubagentRun | undefined = top.requester_session_key
      ? parentByChildSession.get(top.requester_session_key)
      : undefined;
    if (!parent) break;
    top = parent;
  }

  // Collect all depth-1 root tasks under that calling session
  const callingSid = top.requester_session_key ?? top.child_session_key;
  const roots = pool.filter(r => r.depth === 1 && r.requester_session_key === callingSid);
  if (roots.length === 0) return [];

  // For each root task collect its entire subtree (root + descendants), aggregated in BFS order with roots first and descendants after
  const seen = new Set<string>();
  const result: SubagentRun[] = [];
  const appendTree = (root: SubagentRun): void => {
    if (seen.has(root.run_id)) return;
    seen.add(root.run_id);
    result.push(root);
    const queue: string[] = [];
    if (root.child_session_key) queue.push(root.child_session_key);
    while (queue.length > 0) {
      const key = queue.shift();
      if (!key) continue;
      const children = byRequester.get(key);
      if (!children) continue;
      for (const child of children) {
        if (seen.has(child.run_id)) continue;
        seen.add(child.run_id);
        result.push(child);
        if (child.child_session_key) queue.push(child.child_session_key);
      }
    }
  };
  for (const r of roots) appendTree(r);
  return result;
});

/* ------------------------------------------------------------------ */
/* Public API                                                             */
/* ------------------------------------------------------------------ */

export function useSubagentTasks() {
  const { t } = useI18n();

  /** Status badge styling for a run record (colored by ExecutionStatus / RunOutcomeStatus) */
  function badgeClass(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (exec === 'RUNNING') return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
    if (exec === 'INTERRUPTED') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
    const outcome = run?.execution?.outcome?.status;
    if (outcome === 'OK') return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
    if (outcome === 'ERROR') return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
    if (outcome === 'TIMEOUT' || outcome === 'KILLED')
      return 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300';
    return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300';
  }

  /** Status label text (running state first, then delivery state, finally result state) */
  function statusLabel(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (exec === 'RUNNING') return t('sidebar.statusRunning');
    if (exec === 'INTERRUPTED') return t('sidebar.statusInterrupted');
    const delivery = run?.delivery?.status;
    if (delivery === 'PENDING') return t('sidebar.statusPending');
    if (delivery === 'IN_PROGRESS') return t('sidebar.statusInProgress');
    if (delivery === 'DELIVERED') return t('sidebar.statusDelivered');
    const outcome = run?.execution?.outcome?.status;
    if (outcome === 'OK') return t('sidebar.statusDone');
    if (outcome === 'ERROR') return t('sidebar.statusError');
    if (outcome === 'TIMEOUT') return t('sidebar.statusTimeout');
    if (outcome === 'KILLED') return t('sidebar.statusKilled');
    return t('sidebar.statusUnknown');
  }

  /** Role label: root / direct subtask etc. */
  function roleLabel(run: SubagentRun): string {
    const depth = run?.depth ?? 0;
    if (depth <= 0) return t('sidebar.roleRoot');
    return `${t('sidebar.roleChild')}#${depth}`;
  }

  /** Run entry main title: label/task_name first, then the task text */
  function runLabel(run: SubagentRun): string {
    return run?.label || run?.task_name || run?.task || run?.run_id || '-';
  }

  /** Calling session: shows the parent session_id (requester_session_key) that spawned this subtask */
  function parentSessionLabel(run: SubagentRun): string {
    return run?.requester_session_key || '-';
  }

  /** Last-updated time text (second granularity) */
  const lastUpdatedText = computed(() => {
    if (!lastTasksFetchedAt.value) return '';
    const sec = Math.max(0, Math.floor((Date.now() - lastTasksFetchedAt.value) / 1000));
    return t('sidebar.tasksAgoSeconds', { sec });
  });

  /**
   * Initialization: subscribe to WS (singleton) + fetch the current session's tasks.
   * Each consuming component just calls this in onMounted (idempotent). Pass the current
   * sid for the first load.
   */
  function initTasks(sid?: string): void {
    setupSubagentWs();
    // Fetch the set of "still existing" sessions to get orphaned-run filtering ready (idempotent)
    void loadSubagentValidSessions();
    const target = resolveSid(sid);
    if (target && lastLoadedSessionId !== target) {
      lastLoadedSessionId = target;
      void loadTaskRuns(target);
    } else if (target && taskRuns.value.length === 0) {
      void loadTaskRuns(target);
    } else if (!target) {
      // No sid (root path): clear the list
      taskRuns.value = [];
    }
  }

  /** Manually trigger a load (e.g. called by the parent component after switching sessions). */
  function refresh(sid?: string): void {
    const target = resolveSid(sid);
    if (target) {
      lastLoadedSessionId = target;
      void loadTaskRuns(target);
    }
  }

  /**
   * Refresh only the graph data of the currently focused task box (the focused run's subtree).
   *
   * 1) If a run is currently focused: fetch that run + its entire subtree (GET /subagents/runs?run_id=…,
   *    the backend returns root + descendants), then write these records back to the Dexie cache and
   *    rebuild the lists; focusedSubtreeRuns recomputes automatically, so only the currently focused
   *    subtree is redrawn instead of the whole session.
   * 2) If nothing is focused (graph shows the whole tree): fall back to a full refresh for the
   *    current session.
   */
  async function refreshFocusedSubtree(): Promise<void> {
    const rootId = focusedRunId.value;
    // When not focused, fall back to a session-level full refresh
    if (!rootId) {
      refresh(resolveSid());
      return;
    }
    taskLoading.value = true;
    try {
      const runs = await fetchSubagentRunSubtree(rootId);
      if (!runs.length) {
        // The run no longer exists (cleaned up / deleted / expired): exit focus and fall back to the session-level full view, avoiding getting stuck on an empty graph
        focusedRunId.value = undefined;
        selectedRunId.value = undefined;
        refresh(resolveSid());
        return;
      }
      // Write the subtree records back to Dexie (later WS events / other session views then get the latest state too)
      await cacheSubagentRuns(runs.map(toCachedSubagentRun));
      // Rebuild the lists (read the full cache → sync allTaskRuns; focusedSubtreeRuns recomputes from it)
      await refreshFromCache();
      lastTasksFetchedAt.value = Date.now();
    } catch (e) {
      console.error('[useSubagentTasks] 刷新聚焦 task box 子树失败：', e);
      // Backend failure (including run-not-found / network-layer failures): likewise exit focus and fall back to the full view, avoiding the UI getting stuck on a dead run
      focusedRunId.value = undefined;
      selectedRunId.value = undefined;
      refresh(resolveSid());
    } finally {
      taskLoading.value = false;
    }
  }

  /**
   * Explicitly mark whether the "background tasks" view is being shown, so the ready event
   * only triggers a full fetch when actually viewed.
   * Set to true when the sidebar switches to tasks / the task view is shown; set back to
   * false when switching to "Sessions".
   */
  function setTasksTabActive(active: boolean): void {
    tasksTabActive.value = active;
  }

  /* ------------------------------------------------------------------ */
  /* Background tasks tab multi-select / select-all / batch delete                                  */
  /* ------------------------------------------------------------------ */

  /** List of currently selectable (first-level direct task) run_ids. */
  function selectableRunIds(): string[] {
    return rootTaskRuns.value.map(r => r.run_id).filter(Boolean);
  }

  /** Whether everything is selected (non-empty and all selected). */
  const allSelected = computed(() => {
    const ids = selectableRunIds();
    return ids.length > 0 && ids.every(id => selectedRunIds.value.has(id));
  });

  /** Whether in an indeterminate (partially selected) state (some selected but not all). */
  const someSelected = computed(() => {
    const ids = selectableRunIds();
    return ids.some(id => selectedRunIds.value.has(id)) && !allSelected.value;
  });

  /** Toggle the selected state of a single task. */
  function toggleTaskSelection(runId: string): void {
    selectedRunIds.value = new Set(selectedRunIds.value);
    if (selectedRunIds.value.has(runId)) selectedRunIds.value.delete(runId);
    else selectedRunIds.value.add(runId);
  }

  /** Select all / deselect all first-level direct tasks. */
  function toggleSelectAllTasks(): void {
    const ids = selectableRunIds();
    if (allSelected.value) selectedRunIds.value = new Set();
    else selectedRunIds.value = new Set(ids);
  }

  /** Clear the selection (called after deletion completes). */
  function clearTaskSelection(): void {
    selectedRunIds.value = new Set();
  }

  /** Collect the given root run and all its descendant run_ids (parent-child: parent's child_session_key === child's requester_session_key). */
  function collectSubtreeRunIds(rootId: string, pool: SubagentRun[]): string[] {
    const root = pool.find(r => r.run_id === rootId);
    if (!root) return [rootId];
    const out: string[] = [root.run_id];
    const byRequester = new Map<string, SubagentRun[]>();
    for (const run of pool) {
      const key = run.requester_session_key;
      if (!key) continue;
      const list = byRequester.get(key);
      if (list) list.push(run);
      else byRequester.set(key, [run]);
    }
    const queue: string[] = [];
    if (root.child_session_key) queue.push(root.child_session_key);
    while (queue.length > 0) {
      const key = queue.shift();
      if (!key) continue;
      const children = byRequester.get(key);
      if (!children) continue;
      for (const child of children) {
        out.push(child.run_id);
        if (child.child_session_key) queue.push(child.child_session_key);
      }
    }
    return out;
  }

  /**
   * Delete a root task and its entire subtree (fully cleared on both frontend and backend).
   *
   * 1) Call the backend DELETE endpoint to clear the in-memory registry + SQLite + the
   *    attachment directory;
   * 2) Remove the root + all descendants from the store's taskRuns / allTaskRuns;
   * 3) bulkDelete from the Dexie cache so frontend and backend are consistently cleared.
   *
   * @param runId The root run_id to delete.
   */
  async function deleteSubagentSubtree(runId: string): Promise<void> {
    if (deletingRunIds.value.has(runId)) return;
    deletingRunIds.value = new Set(deletingRunIds.value).add(runId);
    try {
      // Pre-compute the whole subtree of ids to remove, based on the current store data
      const pool = allTaskRuns.value;
      const targetIds = collectSubtreeRunIds(runId, pool);
      // 1) Backend deletion
      await deleteSubagentRunSubtree(runId);
      // 2) Remove from the store
      const removed = new Set(targetIds);
      taskRuns.value = taskRuns.value.filter(r => !removed.has(r.run_id));
      allTaskRuns.value = allTaskRuns.value.filter(r => !removed.has(r.run_id));
      // 3) Clear Dexie
      try {
        await deleteCachedSubagentRuns(targetIds);
      } catch (e) {
        console.warn('[useSubagentTasks] 清除本地子任务缓存失败：', e);
      }
      // If the focused / expanded / selected nodes were deleted, clean up the related state too
      if (focusedRunId.value && removed.has(focusedRunId.value)) focusedRunId.value = undefined;
      if (expandedRunId.value && removed.has(expandedRunId.value)) {
        expandedRunId.value = undefined;
        selectedRunId.value = undefined;
      }
      selectedRunIds.value = new Set([...selectedRunIds.value].filter(id => !removed.has(id)));
    } catch (e) {
      console.error('[useSubagentTasks] 删除子 Agent 子树失败：', e);
      throw e;
    } finally {
      deletingRunIds.value = new Set(deletingRunIds.value);
      deletingRunIds.value.delete(runId);
    }
  }

  /** Batch-delete the currently selected first-level tasks (each deletes its own root subtree). */
  async function deleteSelectedTasks(): Promise<number> {
    const ids = [...selectedRunIds.value];
    let removed = 0;
    for (const id of ids) {
      try {
        await deleteSubagentSubtree(id);
        removed += 1;
      } catch {
        // A single failure does not interrupt the deletion of the remaining tasks
      }
    }
    clearTaskSelection();
    return removed;
  }

  return {
    // Reactive state
    taskRuns,
    allTaskRuns,
    rootTaskRuns,
    groupedRootTaskRuns,
    focusedSubtreeRuns,
    taskLoading,
    lastTasksFetchedAt,
    subagentWsReady,
    runningTaskCount,
    allRunningTaskCount,
    lastUpdatedText,
    // Task view expand/flow-graph state (kept alive across chat↔tasks switches)
    expandedRunId,
    selectedRunId,
    focusedRunId,
    toggleExpandRun,
    focusRun,
    resetFlowState,
    // Behavior methods
    isRunning,
    badgeClass,
    statusLabel,
    roleLabel,
    runLabel,
    parentSessionLabel,
    initTasks,
    refresh,
    refreshFocusedSubtree,
    setTasksTabActive,
    // Background tasks tab multi-select / select-all / batch delete
    selectedRunIds,
    deletingRunIds,
    allSelected,
    someSelected,
    toggleTaskSelection,
    toggleSelectAllTasks,
    clearTaskSelection,
    deleteSubagentSubtree,
    deleteSelectedTasks,
    // Lower-level reuse (for SubagentTasksView etc. to do their own internal handling)
    loadTaskRuns,
    refreshFromCache,
    toSubagentRun,
    // Session key normalization + valid-session set (for "back to session" button checks + orphaned-run filtering reuse)
    normalizeSessionKey,
    loadSubagentValidSessions,
    validSessionIds: subagentValidSessionIds
  };
}
