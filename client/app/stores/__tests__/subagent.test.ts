import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { SubagentRun } from '@/composables/bridge';

const bridgeMocks = vi.hoisted(() => ({
  deleteSubagentRunSubtree: vi.fn(async () => 1)
}));

const dbMocks = vi.hoisted(() => ({
  deleteCachedSubagentRuns: vi.fn(async () => undefined)
}));

vi.mock('@/composables/bridge', () => bridgeMocks);
vi.mock('@/composables/db', () => dbMocks);

import { isRunning } from '~/utils/subagent';
import { useSubagentStore } from '../subagent';

/**
 * Build a minimal SubagentRun fixture.
 * @param overrides
 */
function makeRun(overrides: Partial<Omit<SubagentRun, 'run_id'>> & { run_id: string }): SubagentRun {
  return {
    task_run_id: null,
    child_session_key: '',
    requester_session_key: 'session-1',
    task: '',
    task_name: undefined,
    label: undefined,
    spawn_mode: undefined,
    context_mode: undefined,
    agent_id: undefined,
    depth: 1,
    execution: { status: 'DONE', started_at: null, ended_at: null, outcome: { status: 'OK', error: null } },
    completion: { required: false, result_text: null, captured_at: null },
    delivery: { status: 'DELIVERED' },
    ...overrides
  };
}

let store: ReturnType<typeof useSubagentStore>;

describe('stores/subagent', () => {
  beforeEach(() => {
    bridgeMocks.deleteSubagentRunSubtree.mockReset().mockResolvedValue(1);
    dbMocks.deleteCachedSubagentRuns.mockReset().mockResolvedValue(undefined);
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useSubagentStore();
  });

  it('returns the same singleton instance for every caller and exposes empty defaults', () => {
    expect(useSubagentStore()).toBe(store);
    expect(store.taskRuns).toEqual([]);
    expect(store.allTaskRuns).toEqual([]);
    expect(store.taskLoading).toBe(false);
    expect(store.lastTasksFetchedAt).toBe(0);
    expect(store.subagentWsReady).toBe(false);
    expect(store.tasksTabActive).toBe(false);
    expect(store.runningTaskCount).toBe(0);
    expect(store.allRunningTaskCount).toBe(0);
    expect(store.selectedRunIds.size).toBe(0);
    expect(store.deletingRunIds.size).toBe(0);
  });

  it('isRunning treats RUNNING and INTERRUPTED as unfinished', () => {
    expect(isRunning(makeRun({ run_id: 'r1' }))).toBe(false);
    expect(
      isRunning(makeRun({ run_id: 'r2', execution: { status: 'RUNNING', started_at: 0, ended_at: 0, outcome: null } }))
    ).toBe(true);
    expect(
      isRunning(
        makeRun({ run_id: 'r3', execution: { status: 'INTERRUPTED', started_at: 0, ended_at: 0, outcome: null } })
      )
    ).toBe(true);
    expect(store.runningTaskCount).toBe(0);
  });

  it('counts running tasks per session and globally', () => {
    store.taskRuns = [
      makeRun({ run_id: 'r1', execution: { status: 'RUNNING', started_at: 0, ended_at: 0, outcome: null } })
    ];
    store.allTaskRuns = [
      makeRun({ run_id: 'r1', execution: { status: 'RUNNING', started_at: 0, ended_at: 0, outcome: null } }),
      makeRun({ run_id: 'r2' })
    ];
    expect(store.runningTaskCount).toBe(1);
    expect(store.allRunningTaskCount).toBe(1);
  });

  it('rootTaskRuns only keeps depth===1 and groupedRootTaskRuns clusters by session', () => {
    store.allTaskRuns = [
      makeRun({ run_id: 'root-1', depth: 1, requester_session_key: 'A' }),
      makeRun({ run_id: 'root-2', depth: 1, requester_session_key: 'B' }),
      makeRun({ run_id: 'root-3', depth: 1, requester_session_key: 'A' }),
      makeRun({ run_id: 'deep', depth: 2, requester_session_key: 'A' })
    ];
    expect(store.rootTaskRuns.map(r => r.run_id)).toEqual(['root-1', 'root-2', 'root-3']);
    const groups = store.groupedRootTaskRuns;
    expect(groups.map(g => g.sessionId)).toEqual(['A', 'B']);
    expect(groups[0]!.runs.map(r => r.run_id)).toEqual(['root-1', 'root-3']);
  });

  it('focusedSubtreeRuns without focus returns rootTaskRuns; with focus collects the subtree', () => {
    store.allTaskRuns = [
      makeRun({ run_id: 'root-1', depth: 1, child_session_key: 'child-sess' }),
      makeRun({ run_id: 'leaf', depth: 2, requester_session_key: 'child-sess' })
    ];
    expect(store.focusedSubtreeRuns.map(r => r.run_id)).toEqual(['root-1']);

    store.focusRun('leaf');
    expect(store.focusedSubtreeRuns.map(r => r.run_id)).toEqual(['root-1', 'leaf']);
  });

  it('toggleExpandRun expands and collapses a run id (syncing selection)', () => {
    store.toggleExpandRun('r1');
    expect(store.expandedRunId).toBe('r1');
    expect(store.selectedRunId).toBe('r1');

    store.toggleExpandRun('r1');
    expect(store.expandedRunId).toBeUndefined();
    expect(store.selectedRunId).toBeUndefined();
  });

  it('focusRun sets expanded/selected/focused; resetFlowState clears expanded but keeps focused', () => {
    store.focusRun(undefined);
    expect(store.focusedRunId).toBeUndefined();

    store.focusRun('r2');
    expect(store.expandedRunId).toBe('r2');
    expect(store.selectedRunId).toBe('r2');
    expect(store.focusedRunId).toBe('r2');

    store.resetFlowState();
    expect(store.expandedRunId).toBeUndefined();
    expect(store.selectedRunId).toBeUndefined();
    expect(store.focusedRunId).toBe('r2');
  });

  it('toggleSelectAllTasks / toggleTaskSelection / allSelected / someSelected only consider depth-1 roots', () => {
    store.allTaskRuns = [
      makeRun({ run_id: 'root-1', depth: 1 }),
      makeRun({ run_id: 'root-2', depth: 1 }),
      makeRun({ run_id: 'deep', depth: 2 })
    ];
    expect(store.selectableRunIds).toEqual(['root-1', 'root-2']);
    expect(store.allSelected).toBe(false);
    expect(store.someSelected).toBe(false);

    store.toggleSelectAllTasks();
    expect(store.selectedRunIds.has('deep')).toBe(false);
    expect(store.allSelected).toBe(true);
    expect(store.someSelected).toBe(false);

    store.toggleTaskSelection('root-1');
    expect(store.selectedRunIds.has('root-1')).toBe(false);
    expect(store.allSelected).toBe(false);
    expect(store.someSelected).toBe(true);

    store.toggleTaskSelection('root-1');
    store.toggleSelectAllTasks();
    expect(store.selectedRunIds.size).toBe(0);
    expect(store.allSelected).toBe(false);
  });

  it('clearTaskSelection empties the selected set', () => {
    store.allTaskRuns = [makeRun({ run_id: 'root-1', depth: 1 })];
    store.toggleSelectAllTasks();
    store.clearTaskSelection();
    expect(store.selectedRunIds.size).toBe(0);
  });

  it('deleteSubagentSubtree removes run + descendants from store and Dexie and clears flow state', async () => {
    store.allTaskRuns = [
      makeRun({ run_id: 'root-1', depth: 1, child_session_key: 'cs' }),
      makeRun({ run_id: 'child-1', depth: 2, requester_session_key: 'cs' }),
      makeRun({ run_id: 'keep-1', depth: 1 })
    ];
    store.taskRuns = [...store.allTaskRuns];
    store.focusRun('root-1');

    await store.deleteSubagentSubtree('root-1');

    expect(bridgeMocks.deleteSubagentRunSubtree).toHaveBeenCalledWith('root-1');
    expect(dbMocks.deleteCachedSubagentRuns).toHaveBeenCalledWith(['root-1', 'child-1']);
    expect(store.allTaskRuns.map(r => r.run_id)).toEqual(['keep-1']);
    expect(store.taskRuns.map(r => r.run_id)).toEqual(['keep-1']);
    expect(store.focusedRunId).toBeUndefined();
    expect(store.expandedRunId).toBeUndefined();
    expect(store.deletingRunIds.has('root-1')).toBe(false);
  });

  it('deleteSelectedTasks deletes each selected root and returns the count', async () => {
    store.allTaskRuns = [
      makeRun({ run_id: 'root-1', depth: 1 }),
      makeRun({ run_id: 'root-2', depth: 1 }),
      makeRun({ run_id: 'keep', depth: 1 })
    ];
    store.taskRuns = [...store.allTaskRuns];
    store.toggleSelectAllTasks();
    store.toggleTaskSelection('keep');

    const removed = await store.deleteSelectedTasks();
    expect(removed).toBe(2);
    expect(store.allTaskRuns.map(r => r.run_id)).toEqual(['keep']);
    expect(store.selectedRunIds.size).toBe(0);
  });
});
