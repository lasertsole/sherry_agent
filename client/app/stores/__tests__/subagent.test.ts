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
});
