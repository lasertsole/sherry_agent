/**
 * `useSubagentActivity`: run lookup, child-session derivation, steerability and
 * the steer round-trip (the steered record is merged into the shared lists).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { reactive, ref } from 'vue';

const bridge = vi.hoisted(() => ({ steerSubagentRun: vi.fn() }));
vi.mock('@/composables/bridge/session', () => bridge);

const storeState = vi.hoisted(() => ({ store: null as Record<string, unknown> | null }));
vi.mock('~/stores/subagent', () => ({ useSubagentStore: () => storeState.store }));

import { useSubagentActivity } from '../use-subagent-activity';

/**
 * A minimal run record.
 * @param overrides
 */
function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'run-1',
    child_session_key: 'agent:subagent:child-1',
    requester_session_key: 'agent:main:session:parent',
    task: 'search the codebase',
    execution: { status: 'RUNNING', started_at: 1000, ended_at: null, outcome: null },
    completion: { required: false, result_text: null, captured_at: null },
    delivery: { status: 'NONE' },
    ...overrides
  };
}

/**
 * Rebuild the mocked store with the given run lists.
 * @param runs
 */
function setStore(runs: ReturnType<typeof makeRun>[]) {
  storeState.store = reactive({ allTaskRuns: [...runs], taskRuns: [...runs] });
}

describe('useSubagentActivity', () => {
  beforeEach(() => {
    bridge.steerSubagentRun.mockReset();
    setStore([makeRun()]);
  });

  it('finds the run in the global list and derives its child session', () => {
    const activity = useSubagentActivity(ref('run-1'));

    expect(activity.run.value?.run_id).toBe('run-1');
    expect(activity.childSessionKey.value).toBe('agent:subagent:child-1');
  });

  it('answers null for an unknown or absent run id', () => {
    const unknown = useSubagentActivity(ref('nope'));
    expect(unknown.run.value).toBeNull();
    expect(unknown.childSessionKey.value).toBeNull();
    expect(unknown.isSteerable.value).toBe(false);

    const absent = useSubagentActivity(ref(null));
    expect(absent.run.value).toBeNull();
  });

  it('falls back to the session list when the global list is empty', () => {
    storeState.store = reactive({ allTaskRuns: [], taskRuns: [makeRun()] });
    const activity = useSubagentActivity(ref('run-1'));
    expect(activity.run.value?.run_id).toBe('run-1');
  });

  it('is steerable only while the run owns a slot', () => {
    expect(useSubagentActivity(ref('run-1')).isSteerable.value).toBe(true);

    setStore([makeRun({ execution: { status: 'INTERRUPTED', started_at: 1000, ended_at: null, outcome: null } })]);
    expect(useSubagentActivity(ref('run-1')).isSteerable.value).toBe(true);

    setStore([
      makeRun({
        execution: { status: 'DONE', started_at: 1000, ended_at: 2000, outcome: { status: 'OK', error: null } }
      })
    ]);
    expect(useSubagentActivity(ref('run-1')).isSteerable.value).toBe(false);
  });

  it('sends instructions and merges the updated run into both lists', async () => {
    const updated = makeRun({ task: 'updated task' });
    bridge.steerSubagentRun.mockResolvedValue(updated);
    const activity = useSubagentActivity(ref('run-1'));

    await expect(activity.steer('  focus on tests  ')).resolves.toBe(true);

    expect(bridge.steerSubagentRun).toHaveBeenCalledWith('run-1', { new_instructions: 'focus on tests' });
    const store = storeState.store as { allTaskRuns: Array<{ task: string }>; taskRuns: Array<{ task: string }> };
    expect(store.allTaskRuns[0]!.task).toBe('updated task');
    expect(store.taskRuns[0]!.task).toBe('updated task');
  });

  it('refuses an empty instruction or a finished run', async () => {
    const activity = useSubagentActivity(ref('run-1'));
    await expect(activity.steer('   ')).resolves.toBe(false);
    expect(bridge.steerSubagentRun).not.toHaveBeenCalled();

    // The composable resolves the store at creation time, so a NEW activity is
    // what sees the replaced store.
    setStore([
      makeRun({
        execution: { status: 'DONE', started_at: 1000, ended_at: 2000, outcome: { status: 'ERROR', error: 'x' } }
      })
    ]);
    const finished = useSubagentActivity(ref('run-1'));
    await expect(finished.steer('do it')).resolves.toBe(false);
    expect(bridge.steerSubagentRun).not.toHaveBeenCalled();
  });

  it('reports a refused steer as false', async () => {
    bridge.steerSubagentRun.mockResolvedValue(null);
    const activity = useSubagentActivity(ref('run-1'));

    await expect(activity.steer('try this')).resolves.toBe(false);
  });
});
