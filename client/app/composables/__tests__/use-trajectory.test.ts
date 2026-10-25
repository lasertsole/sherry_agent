import { describe, it, expect } from 'vitest';
import { eventMatches, formatDuration, groupTurns, previewOf, summarizeTurns } from '@/composables/use-trajectory';
import type { TrajectoryEvent } from '@/composables/bridge/session';

const event = (overrides: Partial<TrajectoryEvent>): TrajectoryEvent => ({
  event_id: 'e',
  turn_id: 't1',
  seq: 0,
  ts: '2026-10-25T10:00:00',
  kind: 'turn_start',
  is_error: false,
  summary: '',
  payload: {},
  ...overrides
});

describe('use-trajectory', () => {
  it('groups by turn, ordering turns by their first timestamp', () => {
    const turns = groupTurns([
      event({ event_id: 'b', turn_id: 't2', ts: '2026-10-25T10:05:00' }),
      event({ event_id: 'a', turn_id: 't1', ts: '2026-10-25T10:00:00' }),
      event({ event_id: 'c', turn_id: 't2', ts: '2026-10-25T10:06:00' })
    ]);

    expect(turns.map(turn => turn.turnId)).toEqual(['t1', 't2']);
    expect(turns[1]!.events.map(row => row.event_id)).toEqual(['b', 'c']);
  });

  it('sorts a turn by (ts, seq, event_id) so turn_end lands last and lifecycles are not clustered first', () => {
    const turns = groupTurns([
      event({ event_id: 'end', kind: 'turn_end', seq: 0, ts: '2026-10-25T10:00:03' }),
      event({ event_id: 'start', kind: 'turn_start', seq: 0, ts: '2026-10-25T10:00:00' }),
      event({ event_id: 'tool', kind: 'tool_start', seq: 1, ts: '2026-10-25T10:00:01' }),
      event({ event_id: 'retry', kind: 'model_retry', seq: 0, ts: '2026-10-25T10:00:02' })
    ]);

    expect(turns[0]!.events.map(row => row.event_id)).toEqual(['start', 'tool', 'retry', 'end']);
  });

  it('summarizes events, tool calls and tool duration', () => {
    const turns = groupTurns([
      event({ event_id: 'a', kind: 'tool_start', payload: { duration_ms: 12 } }),
      event({ event_id: 'b', kind: 'tool_start', payload: { duration_ms: 30 } }),
      event({ event_id: 'c', kind: 'turn_end' })
    ]);

    expect(summarizeTurns(turns)).toEqual({ eventCount: 3, toolCount: 2, totalToolMs: 42 });
  });

  it('formats durations at the ZCode boundaries', () => {
    expect(formatDuration(820)).toBe('820ms');
    expect(formatDuration(1240)).toBe('1.24s');
    expect(formatDuration(12_300)).toBe('12.3s');
    expect(formatDuration(-1)).toBe('—');
  });

  it('builds kind-specific previews', () => {
    expect(previewOf(event({ kind: 'tool_start', payload: { tool_name: 'read_file', args: { path: 'a.py' } } }))).toBe(
      'read_file {"path":"a.py"}'
    );
    expect(
      previewOf(
        event({
          kind: 'compact',
          payload: { phase: 'pre_request', reason: 'context_limit', messages_before: 10, messages_after: 4 }
        })
      )
    ).toBe('pre_request/context_limit, 10→4');
    expect(
      previewOf(
        event({
          kind: 'model_retry',
          payload: { attempt: 2, max_attempts: 11, status: 'scheduled', reason: 'rate_limit', delay_ms: 1000 }
        })
      )
    ).toBe('2/11 · scheduled · rate_limit · 1000ms');
    expect(previewOf(event({ kind: 'turn_end', payload: { total_ms: 1200, ttfb_ms: 300 } }))).toBe(
      'total 1.20s · ttfb 300ms'
    );
  });

  it('matches queries over summary, kind and payload case-insensitively', () => {
    const row = event({ kind: 'tool_start', summary: 'read_file', payload: { args: { path: 'README.md' } } });

    expect(eventMatches(row, 'readme')).toBe(true);
    expect(eventMatches(row, 'TOOL_START')).toBe(true);
    expect(eventMatches(row, 'missing')).toBe(false);
    expect(eventMatches(row, '   ')).toBe(true);
  });
});
