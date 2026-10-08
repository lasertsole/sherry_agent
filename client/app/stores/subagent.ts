import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import type { SubagentRun } from '~/composables/bridge';
import { logUtil } from '~/utils/log';
import { collectSubtreeRunIds, computeFocusedSubtreeRuns, isRunning } from '~/utils/subagent';

/**
 * Background-task state store: the shared singleton reactive lists, the
 * expand/focus state of the task view, and the self-contained
 * derived views over them.
 *
 * The store is a singleton by construction: every component gets the same
 * reactive state, and the WS subscription / Dexie sync live in
 * `composables/subagent-sync.ts` (which calls into this store).
 */

export const useSubagentStore = defineStore('subagent', () => {
  // ── State ────────────────────────────────────────────────────────────────

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
  /** Whether the "background tasks" view is currently shown (true while the sidebar tasks tab or the right-side task view is active) */
  const tasksTabActive = ref(false);
  /** Currently expanded run_id (clicking a task card header toggles expand/collapse); preserved across chat↔tasks switches */
  const expandedRunId = ref<string | undefined>(undefined);
  /** Selected run of the embedded flow graph (passed through to SubagentFlowGraph's defineModel selected-run-id) */
  const selectedRunId = ref<string | undefined>(undefined);
  /** run_id currently focused in the right-side task view (set when clicking a background task Box on the left; used to display that run's subtree) */
  const focusedRunId = ref<string | undefined>(undefined);
  /** Run ids whose subtree deletion is in flight (guards against a double delete) */
  const deletingRunIds = ref<Set<string>>(new Set());
  /** Set of bare UUIDs of sessions that "still exist".
   *  Data source: the authoritative server `/sessions` list + local Dexie session placeholders; populated by loadSubagentValidSessions().
   *  Purpose: lets SubagentTasksView verify that a run's "back to session" target really exists — if not, that button is hidden. */
  const subagentValidSessionIds = ref<Set<string>>(new Set());

  // ── Derived views ────────────────────────────────────────────────────────

  /**
   * Display list for the right-side task view: with a focused run, all first-level root tasks
   * under that run's calling session plus their full descendant subtrees; without focus, all
   * first-level tasks (see `computeFocusedSubtreeRuns` for the traversal rules).
   */
  const focusedSubtreeRuns = computed<SubagentRun[]>(() =>
    computeFocusedSubtreeRuns(focusedRunId.value, allTaskRuns.value)
  );

  // ── Actions ──────────────────────────────────────────────────────────────

  /**
   * Explicitly mark whether the "background tasks" view is being shown, so the ready event
   * only triggers a full fetch when actually viewed.
   * Set to true when the sidebar switches to tasks / the task view is shown; set back to
   * false when switching to "Sessions".
   * @param active
   */
  function setTasksTabActive(active: boolean): void {
    tasksTabActive.value = active;
  }

  /**
   * Click a task card header: expand/collapse the given run and sync the selected state.
   * @param runId
   */
  function toggleExpandRun(runId: string): void {
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

  /**
   * Toggle the selected state of a single task.
   * @param runId
   */

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
      await deleteRunSubtree(runId);
      // 2) Remove from the store
      const removed = new Set(targetIds);
      taskRuns.value = taskRuns.value.filter(r => !removed.has(r.run_id));
      allTaskRuns.value = allTaskRuns.value.filter(r => !removed.has(r.run_id));
      // 3) Clear Dexie
      try {
        await deleteCachedRuns(targetIds);
      } catch (e) {
        logUtil.w('[useSubagentTasks] Failed to clear local subtask cache:', e);
      }
      // If the focused / expanded / selected nodes were deleted, clean up the related state too
      if (focusedRunId.value && removed.has(focusedRunId.value)) focusedRunId.value = undefined;
      if (expandedRunId.value && removed.has(expandedRunId.value)) {
        expandedRunId.value = undefined;
        selectedRunId.value = undefined;
      }
    } catch (e) {
      logUtil.e('[useSubagentTasks] Failed to delete subagent subtree:', e);
      throw e;
    } finally {
      deletingRunIds.value = new Set(deletingRunIds.value);
      deletingRunIds.value.delete(runId);
    }
  }

  return {
    // State
    taskRuns,
    allTaskRuns,
    taskLoading,
    lastTasksFetchedAt,
    subagentWsReady,
    tasksTabActive,
    expandedRunId,
    selectedRunId,
    focusedRunId,
    deletingRunIds,
    subagentValidSessionIds,
    // Derived
    focusedSubtreeRuns,
    // Behavior
    isRunning,
    setTasksTabActive,
    toggleExpandRun,
    focusRun,
    resetFlowState,
    deleteSubagentSubtree
  };
});
