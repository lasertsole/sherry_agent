import type { SubagentRun } from '~/composables/bridge';

/** Defines the background-task grouping structure after clustering by calling session. */
export interface TaskSessionGroup {
  /** Calling session_id (requester_session_key; empty values fall into the fallback key '-') */
  sessionId: string;
  /** First-level task list spawned under that calling session (keeps the original rootTaskRuns order) */
  runs: SubagentRun[];
}

/**
 * Whether the run is still running (RUNNING / INTERRUPTED count as not yet finished)
 * @param run
 */
export function isRunning(run: SubagentRun): boolean {
  const status = run?.execution?.status;
  return status === 'RUNNING' || status === 'INTERRUPTED';
}

/**
 * Collect the given root run and all its descendant run_ids (parent-child: parent's child_session_key === child's requester_session_key).
 * @param rootId
 * @param pool
 */
export function collectSubtreeRunIds(rootId: string, pool: SubagentRun[]): string[] {
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
 * Clustered list for the background tasks tab: groups task boxes by "calling session_id"
 * (the requester_session_key of each root task). Multiple root tasks spawned by the same
 * calling session are placed into the same group, so the left column can show intuitive
 * session-based clusters; groups are stably sorted by sessionId (the empty-value group
 * goes last).
 * @param rootTaskRuns
 */
export function groupRunsBySession(rootTaskRuns: SubagentRun[]): TaskSessionGroup[] {
  const groups = new Map<string, SubagentRun[]>();
  for (const run of rootTaskRuns) {
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
}

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
 * @param rootId Focused run_id (undefined = no focus).
 * @param pool The full run list across all sessions.
 */
export function computeFocusedSubtreeRuns(rootId: string | undefined, pool: SubagentRun[]): SubagentRun[] {
  if (!rootId) return pool.filter(run => run?.depth === 1);

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
}
