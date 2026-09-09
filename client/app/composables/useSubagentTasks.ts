/**
 * Shared "background tasks" state composable (module-level singleton)
 *
 * Facade over the per-concern slices: `subagentState` (reactive lists +
 * computed views), `subagentTree` (expand/focus/flow-graph state),
 * `subagentSelection` (multi-select + batch delete), `subagentSync`
 * (fetch/cache rebuild/WS subscription) and `subagentRepository`
 * (bridge/db data access + schema mapping).
 *
 * Responsibility: centrally manages all state plus the loading/WS/Dexie caching
 * logic for subagent run records (taskRuns), so that the left sidebar (SessionSidebar.vue)
 * and the right full task list view (SubagentTasksView.vue) share the same reactive
 * data and stay consistent in real time without duplicate subscriptions.
 */
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SubagentRun } from './bridge';

/**
 * Create the background-tasks API. Every consumer gets the same module-level
 * singleton state; only the i18n-bound label helpers are per-call.
 */
export function useSubagentTasks() {
  const { t } = useI18n();

  /**
   * Status badge styling for a run record (colored by ExecutionStatus / RunOutcomeStatus)
   * @param run
   */
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

  /**
   * Status label text (running state first, then delivery state, finally result state)
   * @param run
   */
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
