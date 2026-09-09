/**
 * Background-task selection slice: multi-select / select-all / batch delete of
 * first-level direct tasks on the "background tasks" tab.
 */
import { computed, ref } from 'vue';
import type { SubagentRun } from './bridge';
import { logUtil } from '~/utils/log';

/** Set of root run_ids currently selected in the background tasks tab (for multi-select / select-all / batch delete) */
export const selectedRunIds = ref<Set<string>>(new Set());
/** Batch deletion currently in progress on the background tasks tab */
export const deletingRunIds = ref<Set<string>>(new Set());

/** List of currently selectable (first-level direct task) run_ids. */
export function selectableRunIds(): string[] {
  return rootTaskRuns.value.map(r => r.run_id).filter(Boolean);
}

/** Whether everything is selected (non-empty and all selected). */
export const allSelected = computed(() => {
  const ids = selectableRunIds();
  return ids.length > 0 && ids.every(id => selectedRunIds.value.has(id));
});

/** Whether in an indeterminate (partially selected) state (some selected but not all). */
export const someSelected = computed(() => {
  const ids = selectableRunIds();
  return ids.some(id => selectedRunIds.value.has(id)) && !allSelected.value;
});

/**
 * Toggle the selected state of a single task.
 * @param runId
 */
export function toggleTaskSelection(runId: string): void {
  selectedRunIds.value = new Set(selectedRunIds.value);
  if (selectedRunIds.value.has(runId)) selectedRunIds.value.delete(runId);
  else selectedRunIds.value.add(runId);
}

/** Select all / deselect all first-level direct tasks. */
export function toggleSelectAllTasks(): void {
  const ids = selectableRunIds();
  if (allSelected.value) selectedRunIds.value = new Set();
  else selectedRunIds.value = new Set(ids);
}

/** Clear the selection (called after deletion completes). */
export function clearTaskSelection(): void {
  selectedRunIds.value = new Set();
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
 * Delete a root task and its entire subtree (fully cleared on both frontend and backend).
 *
 * 1) Call the backend DELETE endpoint to clear the in-memory registry + SQLite + the
 *    attachment directory;
 * 2) Remove the root + all descendants from the store's taskRuns / allTaskRuns;
 * 3) bulkDelete from the Dexie cache so frontend and backend are consistently cleared.
 *
 * @param runId The root run_id to delete.
 */
export async function deleteSubagentSubtree(runId: string): Promise<void> {
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
    selectedRunIds.value = new Set([...selectedRunIds.value].filter(id => !removed.has(id)));
  } catch (e) {
    logUtil.e('[useSubagentTasks] Failed to delete subagent subtree:', e);
    throw e;
  } finally {
    deletingRunIds.value = new Set(deletingRunIds.value);
    deletingRunIds.value.delete(runId);
  }
}

/** Batch-delete the currently selected first-level tasks (each deletes its own root subtree). */
export async function deleteSelectedTasks(): Promise<number> {
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
