/**
 * Session filtering of the cached run list.
 *
 * Runs are recorded against the prefixed announcer key
 * (``agent:main:session:{id}``) while the page works with the bare session id,
 * so the filter has to compare normalized forms — a raw comparison hides every
 * run of the session (including the active ones the toolbar entry lists).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { filterBySession, normalizeSessionKey } from '../subagent-sync';
import type { CachedSubagentRun } from '../db';

/**
 * One cached run record for the given requester key.
 * @param runId
 * @param requester
 */
const cached = (runId: string, requester: string | null): CachedSubagentRun =>
  ({
    run_id: runId,
    requester_session_key: requester,
    child_session_key: `agent:main:subagent:${runId}`,
    task: 'do the thing',
    execution: { status: 'running', outcome: null, started_at: '1', completed_at: null }
  }) as unknown as CachedSubagentRun;

describe('subagent-sync session filtering', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('normalizes both prefixed and bare session keys', () => {
    expect(normalizeSessionKey('agent:main:session:e2e-demo')).toBe('e2e-demo');
    expect(normalizeSessionKey('agent:subagent:abc')).toBe('abc');
    expect(normalizeSessionKey('e2e-demo')).toBe('e2e-demo');
    expect(normalizeSessionKey(null)).toBeNull();
  });

  it('matches a run recorded under the prefixed key to the bare session id', () => {
    const runs = filterBySession(
      [cached('r1', 'agent:main:session:e2e-demo'), cached('r2', 'agent:main:session:other')],
      'e2e-demo'
    );

    expect(runs.map(r => r.run_id)).toEqual(['r1']);
  });

  it('accepts a prefixed session id as the target too', () => {
    const runs = filterBySession([cached('r1', 'agent:main:session:e2e-demo')], 'agent:main:session:e2e-demo');

    expect(runs.map(r => r.run_id)).toEqual(['r1']);
  });

  it('returns nothing without a session id', () => {
    expect(filterBySession([cached('r1', 'agent:main:session:e2e-demo')], undefined)).toEqual([]);
  });
});
