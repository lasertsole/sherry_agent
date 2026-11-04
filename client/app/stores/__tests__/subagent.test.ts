import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { SubagentRun } from '@/composables/bridge';

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
    expect(store.subagentValidSessionIds.size).toBe(0);
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
});
