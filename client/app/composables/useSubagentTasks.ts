/**
 * Shared "background tasks" composable — thin facade over `stores/subagent.ts`.
 *
 * Reactive state, derived views and flow/selection actions live in the Pinia
 * store; this facade keeps the established consumer shape (refs + actions) and
 * owns the i18n-bound presentation helpers (`badgeClass` / `statusLabel` / …),
 * which need the calling component's translator and therefore stay per call.
 *
 * The fetch/cache/WS logic itself lives in `subagent-sync.ts` and resolves the
 * store internally.
 */
import { computed } from 'vue';
import { storeToRefs } from 'pinia';
import { useI18n } from 'vue-i18n';
import { useSubagentStore } from '~/stores/subagent';
import { subagentStatusMeta } from '~/utils/subagent-status';
import type { SubagentRun } from './bridge';

/**
 * Create the background-tasks API. Every consumer gets the same store-backed
 * state; only the i18n-bound label helpers are per-call.
 */
export function useSubagentTasks() {
  const { t } = useI18n();
  const store = useSubagentStore();
  const {
    taskRuns,
    allTaskRuns,
    taskLoading,
    lastTasksFetchedAt,
    subagentWsReady,
    expandedRunId,
    selectedRunId,
    focusedRunId,
    selectedRunIds,
    deletingRunIds,
    subagentValidSessionIds,
    runningTaskCount,
    allRunningTaskCount,
    rootTaskRuns,
    groupedRootTaskRuns,
    focusedSubtreeRuns,
    allSelected,
    someSelected
  } = storeToRefs(store);

  /**
   * Status badge styling for a run record (colored by ExecutionStatus / RunOutcomeStatus)
   * @param run
   */
  function badgeClass(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (exec === 'RUNNING' || exec === 'INTERRUPTED') return subagentStatusMeta(exec).badgeClass;
    return subagentStatusMeta(run?.execution?.outcome?.status).badgeClass;
  }

  /**
   * Status label text (running state first, then delivery state, finally result state)
   * @param run
   */
  function statusLabel(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (exec === 'RUNNING' || exec === 'INTERRUPTED') return t(subagentStatusMeta(exec).labelKey);
    const delivery = run?.delivery?.status;
    if (delivery === 'PENDING' || delivery === 'IN_PROGRESS' || delivery === 'DELIVERED') {
      return t(subagentStatusMeta(delivery).labelKey);
    }
    return t(subagentStatusMeta(run?.execution?.outcome?.status).labelKey);
  }

  /**
   * Role label: root / direct subtask etc.
   * @param run
   */
  function roleLabel(run: SubagentRun): string {
    const depth = run?.depth ?? 0;
    if (depth <= 0) return t('sidebar.roleRoot');
    return `${t('sidebar.roleChild')}#${depth}`;
  }

  /**
   * Run entry main title: label/task_name first, then the task text
   * @param run
   */
  function runLabel(run: SubagentRun): string {
    return run?.label || run?.task_name || run?.task || run?.run_id || '-';
  }

  /**
   * Calling session: shows the parent session_id (requester_session_key) that spawned this subtask
   * @param run
   */
  function parentSessionLabel(run: SubagentRun): string {
    return run?.requester_session_key || '-';
  }

  /** Last-updated time text (second granularity) */
  const lastUpdatedText = computed(() => {
    if (!lastTasksFetchedAt.value) return '';
    const sec = Math.max(0, Math.floor((Date.now() - lastTasksFetchedAt.value) / 1000));
    return t('sidebar.tasksAgoSeconds', { sec });
  });

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
    toggleExpandRun: store.toggleExpandRun,
    focusRun: store.focusRun,
    resetFlowState: store.resetFlowState,
    // Behavior methods
    isRunning: store.isRunning,
    badgeClass,
    statusLabel,
    roleLabel,
    runLabel,
    parentSessionLabel,
    initTasks,
    refresh,
    refreshFocusedSubtree,
    setTasksTabActive: store.setTasksTabActive,
    // Background tasks tab multi-select / select-all / batch delete
    selectedRunIds,
    deletingRunIds,
    allSelected,
    someSelected,
    toggleTaskSelection: store.toggleTaskSelection,
    toggleSelectAllTasks: store.toggleSelectAllTasks,
    clearTaskSelection: store.clearTaskSelection,
    deleteSubagentSubtree: store.deleteSubagentSubtree,
    deleteSelectedTasks: store.deleteSelectedTasks,
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
