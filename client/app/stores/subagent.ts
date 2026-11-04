import { defineStore } from 'pinia';
import { ref } from 'vue';
import type { SubagentRun } from '~/composables/bridge';
import { isRunning } from '~/utils/subagent';

/**
 * Background-task state store: the shared singleton reactive lists.
 *
 * The store is a singleton by construction: every component gets the same
 * reactive state, and the WS subscription / Dexie sync live in
 * `composables/subagent-sync.ts` (which calls into this store).
 *
 * The task-DETAIL tab (tree + flow graph + run detail pane) was removed; the
 * lists stay because the toolbar's tasks button (its active badge + popover)
 * reads them and the planned activity view reuses them.
 */

export const useSubagentStore = defineStore('subagent', () => {
  // ── State ────────────────────────────────────────────────────────────────

  /** Subagent run record list (filtered to the current session; used by the chat page jump bar / sidebar red dot to detect this session's tasks) */
  const taskRuns = ref<SubagentRun[]>([]);
  /** Global subagent run record list (all sessions included; used by the toolbar's tasks button to show tasks from every session) */
  const allTaskRuns = ref<SubagentRun[]>([]);
  /** Background tasks currently loading */
  const taskLoading = ref(false);
  /** Timestamp of the last successful fetch (ms) */
  const lastTasksFetchedAt = ref<number>(0);
  /** Whether a "background tasks" realtime message has ever been received (avoids duplicate full fetches on first paint / reconnect) */
  const subagentWsReady = ref(false);
  /** Set of bare UUIDs of sessions that "still exist".
   *  Data source: the authoritative server `/sessions` list + local Dexie session placeholders; populated by loadSubagentValidSessions().
   *  Purpose: lets a consumer hide a "back to session" target that no longer exists. */
  const subagentValidSessionIds = ref<Set<string>>(new Set());
  /** run_id the agent-activity tab currently shows (null when no tab is open). */
  const activeActivityRunId = ref<string | null>(null);

  /**
   * Point the activity tab at one run (null clears it when the tab closes).
   * @param runId Run under watch, or null.
   */
  function setActiveActivityRun(runId: string | null): void {
    activeActivityRunId.value = runId;
  }

  return {
    // State
    taskRuns,
    allTaskRuns,
    taskLoading,
    lastTasksFetchedAt,
    subagentWsReady,
    subagentValidSessionIds,
    activeActivityRunId,
    // Behavior
    isRunning,
    setActiveActivityRun
  };
});
