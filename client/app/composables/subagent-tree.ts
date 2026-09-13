/**
 * Background-task tree slice: expand/selection/focus state of the task view
 * (kept alive across chat↔tasks switches) and the focused-subtree computation.
 */
import { computed, ref } from 'vue';
import type { SubagentRun } from './bridge';

/** Currently expanded run_id (clicking a task card header toggles expand/collapse); preserved across chat↔tasks switches */
export const expandedRunId = ref<string | undefined>(undefined);
/** Selected run of the embedded flow graph (passed through to SubagentFlowGraph's defineModel selected-run-id) */
export const selectedRunId = ref<string | undefined>(undefined);
/** run_id currently focused in the right-side task view (set when clicking a background task Box on the left; used to display that run's subtree) */
export const focusedRunId = ref<string | undefined>(undefined);

/**
 * Click a task card header: expand/collapse the given run and sync the selected state.
 * @param runId
 */
export function toggleExpandRun(runId: string): void {
  if (expandedRunId.value === runId) {
    expandedRunId.value = undefined;
    selectedRunId.value = undefined;
  } else {
    expandedRunId.value = runId;
    selectedRunId.value = runId;
  }
}

/**
 * Focus/expand the given run (called when clicking a sidebar task item); if found, expand it and sync the selected state.
 * @param runId
 */
export function focusRun(runId: string | undefined): void {
  if (!runId) return;
  expandedRunId.value = runId;
  selectedRunId.value = runId;
  // Also record the run focused by the right-side view, used to display that run's subtree
  focusedRunId.value = runId;
}

/** When switching back from the task view to chat, the expanded state is kept rather than reset (call explicitly to clear it). */
export function resetFlowState(): void {
  expandedRunId.value = undefined;
  selectedRunId.value = undefined;
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
 */
export const focusedSubtreeRuns = computed<SubagentRun[]>(() => {
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
