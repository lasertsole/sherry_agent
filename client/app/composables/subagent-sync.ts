/**
 * Background-task sync slice: server fetch + local cache rebuild + the
 * `/subagents/ws` realtime subscription (singleton), plus the session-existence
 * validation data.
 */
import { ref } from 'vue';
import type { SubagentRun } from './bridge';
import type { CachedSubagentRun } from './db';
import { logUtil } from '~/utils/log';

/** Last initialized session (used to refresh the list when switching sessions) */
let lastLoadedSessionId: string | undefined;

/** Whether the WS subscription has been established (singleton guard, avoids duplicate on() subscriptions) */
let subscribed = false;

/** Set of bare UUIDs of sessions that "still exist".
 *  Data source: the authoritative server `/sessions` list + local Dexie session placeholders; populated by loadSubagentValidSessions().
 *  Purpose: lets SubagentTasksView verify that a run's "back to session" target really exists — if not, that button is hidden. */
export const subagentValidSessionIds = ref<Set<string>>(new Set());

/** Whether it has already been loaded (avoids re-fetching the session list on every session switch). */
let subagentSessionsLoaded = false;

/* Session key prefixes: the backend uses prefixed keys when attributing subtasks to the calling session; display/Nav needs them normalized to bare UUIDs */
const SESSION_KEY_PREFIXES = ['agent:main:session:', 'agent:subagent:'];

/**
 * Normalize a session key: strip the prefix to get the bare UUID; keys without a prefix (already bare/'/'-'/default) are returned as-is.
 * @param key
 */
export function normalizeSessionKey(key: string | null | undefined): string | null {
  if (!key) return null;
  for (const prefix of SESSION_KEY_PREFIXES) {
    if (key.startsWith(prefix)) {
      const bare = key.slice(prefix.length);
      return bare || null;
    }
  }
  return key;
}

/**
 * Fetch and cache the set of sessions that "still exist".
 * Used by SubagentTasksView for "back to session" existence checks: for an orphaned run
 * whose normalized requester_session_key is not in this set, the task box is still shown
 * as usual, but the "back to session" button is hidden.
 * Idempotent: it only actually fetches once (unless cross-window state was cleared;
 * can be explicitly called again).
 */
export async function loadSubagentValidSessions(): Promise<void> {
  if (subagentSessionsLoaded) return;
  try {
    const set = await getExistingSessionIds();
    subagentValidSessionIds.value = set;
    subagentSessionsLoaded = true;
  } catch (error) {
    // A fetch failure must not block the UI: it is equivalent to "unable to confirm the target session", so the button is simply hidden as if there were no valid target.
    logUtil.w('[useSubagentTasks] Failed to fetch session list, cannot validate "back to session" target:', error);
  }
}

/**
 * Resolve the currently active session id.
 * Supports two sources: preferably extracted from the URL pathname (safe at module
 * level, no setup context required), or explicitly passed in by the caller via an
 * argument (recommended, keeps the source consistent with the sidebar/router).
 * @param force
 */
export function resolveSid(force?: string): string | undefined {
  if (force) return force;
  if (typeof window === 'undefined') return undefined;
  const segs = window.location.pathname.split('/').filter(Boolean);
  const sid = segs[segs.length - 1];
  return sid && sid !== 'home' ? sid : undefined;
}

/**
 * Keep only the cached runs that belong to the current session (or its subtasks).
 * @param runs
 * @param sid
 */
export function filterBySession(runs: CachedSubagentRun[], sid: string | undefined): SubagentRun[] {
  if (!sid) return [];
  return runs.filter(c => c.requester_session_key === sid || c.child_session_key === sid).map(toSubagentRun);
}

/**
 * Rebuild the lists from the Dexie cache (local immediate update after WS events / session switches / reconnects).
 * @param sid
 */
export async function refreshFromCache(sid?: string): Promise<void> {
  try {
    const cached = await readCachedRuns();
    // The global cache is cumulative data across "all sessions" (the Dexie table is global); map it directly into the global task view data
    allTaskRuns.value = cached.map(toSubagentRun);
    // Session-filtered view: used by the chat page jump bar / sidebar red dot to detect this session's tasks
    const target = resolveSid(sid);
    taskRuns.value = target ? filterBySession(cached, target) : [];
  } catch {
    // Ignore cache read failures; the next loadTaskRuns acts as the fallback
  }
}

/**
 * Refresh taskRuns: first immediately echo the local cache (works offline), then asynchronously fetch from the backend to fill the gaps.
 * @param sid
 */
export async function loadTaskRuns(sid?: string): Promise<void> {
  const target = resolveSid(sid);
  taskLoading.value = true;
  // 1) Local cache first: read IndexedDB and render immediately, so refreshes / first paint never show an empty window
  try {
    const cached = await readCachedRuns();
    if (target) taskRuns.value = filterBySession(cached, target);
    else taskRuns.value = [];
    // Global view sync: regardless of whether there is a target, the global task list always comes from the full cache
    allTaskRuns.value = cached.map(toSubagentRun);
  } catch (e) {
    logUtil.w('[useSubagentTasks] Failed to read local subtask cache, falling back to server:', e);
  }
  // 2) Server-side gap filling: fetch the whole run tree and write it to Dexie, recovering events missed while the WS was disconnected
  if (target) {
    try {
      const runs = await fetchSessionRuns(target);
      await cacheRuns(runs);
      const cached = await readCachedRuns();
      taskRuns.value = filterBySession(cached, target);
      // After the fetch the full cache is up to date, so refresh the global task list too
      allTaskRuns.value = cached.map(toSubagentRun);
      lastTasksFetchedAt.value = Date.now();
    } catch (e) {
      // Network failure: keep the Dexie cache as fallback instead of clearing the list, avoiding first-paint flicker
      logUtil.e('[useSubagentTasks] Failed to fetch subagent run records (falling back to local cache)', e);
    }
  }
  taskLoading.value = false;
}

/**
 * Establish the /subagents/ws connection and subscribe to realtime events so the
 * background task list updates incrementally and in real time.
 * The subscribed guard ensures the subscription is established only once, no matter
 * how many components call this.
 */
export function setupSubagentWs(): void {
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
    void cacheRuns([run]).then(() => refreshFromCache());
  });

  on('ws:subagent_ended', (payload: unknown) => {
    const run = payload as SubagentRun;
    if (!run?.run_id) return;
    // After the run ends, overwrite with the complete state (including outcome / delivery) for final result display
    void cacheRuns([run]).then(() => refreshFromCache());
  });

  // ready: the server is ready; trigger one full gap-fill (recovers events missed before the connection was established)
  on('ws:subagents:ready', () => {
    subagentWsReady.value = true;
    // Only trigger the full gap-fill while the "background tasks" view is being shown (fetch on actual viewing, avoids pointless requests)
    if (tasksTabActive.value) void loadTaskRuns();
  });
}

/**
 * Initialization: subscribe to WS (singleton) + fetch the current session's tasks.
 * Each consuming component just calls this in onMounted (idempotent). Pass the current
 * sid for the first load.
 * @param sid
 */
export function initTasks(sid?: string): void {
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

/**
 * Manually trigger a load (e.g. called by the parent component after switching sessions).
 * @param sid
 */
export function refresh(sid?: string): void {
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
export async function refreshFocusedSubtree(): Promise<void> {
  const rootId = focusedRunId.value;
  // When not focused, fall back to a session-level full refresh
  if (!rootId) {
    refresh(resolveSid());
    return;
  }
  taskLoading.value = true;
  try {
    const runs = await fetchRunSubtree(rootId);
    if (!runs.length) {
      // The run no longer exists (cleaned up / deleted / expired): exit focus and fall back to the session-level full view, avoiding getting stuck on an empty graph
      focusedRunId.value = undefined;
      selectedRunId.value = undefined;
      refresh(resolveSid());
      return;
    }
    // Write the subtree records back to Dexie (later WS events / other session views then get the latest state too)
    await cacheRuns(runs);
    // Rebuild the lists (read the full cache → sync allTaskRuns; focusedSubtreeRuns recomputes from it)
    await refreshFromCache();
    lastTasksFetchedAt.value = Date.now();
  } catch (e) {
    logUtil.e('[useSubagentTasks] Failed to refresh focused task box subtree:', e);
    // Backend failure (including run-not-found / network-layer failures): likewise exit focus and fall back to the full view, avoiding the UI getting stuck on a dead run
    focusedRunId.value = undefined;
    selectedRunId.value = undefined;
    refresh(resolveSid());
  } finally {
    taskLoading.value = false;
  }
}
