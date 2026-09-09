/**
 * Background-task state slice: the module-level singleton reactive lists and
 * the self-contained computed views over them.
 *
 * Uses module-level state instead of instance-level state: no matter how many
 * components call `useSubagentTasks`, they all get the same refs and the WS
 * subscription is established only once.
 */
import { computed, ref } from 'vue';
import type { SubagentRun } from './bridge';

/** Subagent run record list (filtered to the current session; used by the chat page jump bar / sidebar red dot to detect this session's tasks) */
export const taskRuns = ref<SubagentRun[]>([]);
/** Global subagent run record list (all sessions included; used by the background tasks view to show tasks from every session) */
export const allTaskRuns = ref<SubagentRun[]>([]);
/** Background tasks currently loading */
export const taskLoading = ref(false);
/** Timestamp of the last successful fetch (ms) */
export const lastTasksFetchedAt = ref<number>(0);
/** Whether a "background tasks" realtime message has ever been received (avoids duplicate full fetches on first paint / reconnect) */
export const subagentWsReady = ref(false);
/** Whether the "background tasks" view is currently shown (true while the sidebar tasks tab or the right-side task view is active) */
export const tasksTabActive = ref(false);

/**
 * Explicitly mark whether the "background tasks" view is being shown, so the ready event
 * only triggers a full fetch when actually viewed.
 * Set to true when the sidebar switches to tasks / the task view is shown; set back to
 * false when switching to "Sessions".
 * @param active
 */
export function setTasksTabActive(active: boolean): void {
  tasksTabActive.value = active;
}

/**
 * Whether the run is still running (RUNNING / INTERRUPTED count as not yet finished)
 * @param run
 */
export function isRunning(run: SubagentRun): boolean {
  const status = run?.execution?.status;
  return status === 'RUNNING' || status === 'INTERRUPTED';
}

/** Number of running resident subagents (for the "Sessions" tab red-dot badge; filtered to the current session). */
export const runningTaskCount = computed(() => taskRuns.value.filter(run => isRunning(run)).length);

/** Number of running subagents across all sessions (for the "background tasks" tab red-dot badge). */
export const allRunningTaskCount = computed(() => allTaskRuns.value.filter(run => isRunning(run)).length);

/** Display list for the background tasks tab: only "first-level direct tasks" (depth === 1, i.e. subtasks spawned directly by each session), across all sessions.
 *  Orphaned runs (stale cache whose calling session has already been destroyed) are **still shown**; their "back to session"
 *  target is validated by SubagentTasksView via wouldExistSession, and the button is hidden when it does not exist (the task box is kept). */
export const rootTaskRuns = computed(() => allTaskRuns.value.filter(run => run?.depth === 1));

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
export const groupedRootTaskRuns = computed<TaskSessionGroup[]>(() => {
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
