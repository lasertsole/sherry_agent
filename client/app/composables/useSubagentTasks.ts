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
import { isRunning } from '~/utils/subagent';
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
    deletingRunIds,
    subagentValidSessionIds,
    focusedSubtreeRuns
  } = storeToRefs(store);

  /**
   * Status badge styling for a run record (colored by ExecutionStatus / RunOutcomeStatus)
   * @param run
   */
  function badgeClass(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (isRunning(run)) return subagentStatusMeta(exec).badgeClass;
    return subagentStatusMeta(run?.execution?.outcome?.status).badgeClass;
  }

  /**
   * Status label text (running state first, then delivery state, finally result state)
   * @param run
   */
  function statusLabel(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (isRunning(run)) return t(subagentStatusMeta(exec).labelKey);
    const delivery = String(run?.delivery?.status ?? '').toUpperCase();
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
    // The recorded key is the announcer form (``agent:main:session:{id}``);
    // show the bare id the session list uses.
    return normalizeSessionKey(run?.requester_session_key) || '-';
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
    focusedSubtreeRuns,
    taskLoading,
    lastTasksFetchedAt,
    subagentWsReady,
    lastUpdatedText,
    // Task view expand/flow-graph state (kept alive across tab switches)
    expandedRunId,
    selectedRunId,
    focusedRunId,
    toggleExpandRun: store.toggleExpandRun,
    focusRun: store.focusRun,
    resetFlowState: store.resetFlowState,
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
    setTasksTabActive: store.setTasksTabActive,
    // Single-run deletion (the detail pane's delete action; guards a double delete)
    deletingRunIds,
    deleteSubagentSubtree: store.deleteSubagentSubtree,
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
