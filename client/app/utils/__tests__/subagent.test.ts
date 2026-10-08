/**
 * Background-task predicates: which runs count as still running / still active.
 *
 * The wire payload carries the status vocabulary in either case (HTTP/WS send
 * the backend enums' lower-case values, the native IPC payload upper-cases
 * them), so both must be understood. And a run must not keep sitting in the
 * "running" list after it finished, even when its status field is stale.
 */
import { describe, it, expect } from 'vitest';
import { isActiveRun, isRunning } from '../subagent';
import type { SubagentRun } from '~/composables/bridge';

/**
 * One run record with the given status / terminal markers.
 * @param execution
 * @param extra
 */
const run = (execution: Partial<SubagentRun['execution']> = {}, extra: Partial<SubagentRun> = {}): SubagentRun =>
  ({
    run_id: 'run-1',
    child_session_key: 'agent:main:subagent:1',
    requester_session_key: 'agent:main:session:sid-1',
    task: 'do the thing',
    execution: {
      status: 'running',
      started_at: 1_700_000_000_000,
      ended_at: null,
      outcome: null,
      ...execution
    },
    completion: { required: true, result_text: null, captured_at: null },
    delivery: { status: 'pending' },
    ...extra
  }) as SubagentRun;

describe('isRunning', () => {
  it('understands the lower-case wire values', () => {
    expect(isRunning(run({ status: 'running' }))).toBe(true);
    expect(isRunning(run({ status: 'interrupted' }))).toBe(true);
  });

  it('understands the upper-case payload values', () => {
    expect(isRunning(run({ status: 'RUNNING' }))).toBe(true);
    expect(isRunning(run({ status: 'INTERRUPTED' }))).toBe(true);
  });

  it('is false for a finished run', () => {
    expect(isRunning(run({ status: 'terminal', ended_at: 1_700_000_100_000 }))).toBe(false);
    expect(isRunning(run({ status: 'queued' }))).toBe(false);
    expect(isRunning(run({ status: undefined }))).toBe(false);
  });
});

describe('isActiveRun', () => {
  it('accepts queued, running and interrupted runs in either case', () => {
    expect(isActiveRun(run({ status: 'pending' }))).toBe(true);
    expect(isActiveRun(run({ status: 'RUNNING' }))).toBe(true);
    expect(isActiveRun(run({ status: 'Interrupted' }))).toBe(true);
  });

  it('rejects a finished run', () => {
    expect(isActiveRun(run({ status: 'terminal' }))).toBe(false);
  });

  it('rejects a stale "running" record that already carries an end marker', () => {
    // The subagent_ended frame can be missed (reload, reconnect); the recorded
    // markers are then the only evidence that the run is over.
    expect(isActiveRun(run({ status: 'running', ended_at: 1_700_000_100_000 }))).toBe(false);
    expect(isActiveRun(run({ status: 'running' }, { ended_reason: 'complete' }))).toBe(false);
    expect(isActiveRun(run({ status: 'running', outcome: { status: 'ok', error: null } }))).toBe(false);
    expect(isActiveRun(run({ status: 'running', outcome: { status: 'KILLED', error: null } }))).toBe(false);
  });

  it('keeps an interrupted run that has no terminal marker', () => {
    expect(isActiveRun(run({ status: 'interrupted' }, { pause_reason: 'user_pause' }))).toBe(true);
  });
});
