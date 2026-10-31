/**
 * Pure helpers for the trajectory panel: grouping, ordering, summaries and
 * formatting. No reactive state lives here so each function is directly unit
 * testable.
 */

import type { TrajectoryEvent } from '~/composables/bridge/session';

/** One turn's events, oldest first. */
export interface TrajectoryTurn {
  turnId: string;
  events: TrajectoryEvent[];
}

/** Header summary numbers. */
export interface TrajectorySummary {
  eventCount: number;
  toolCount: number;
  totalToolMs: number;
}

/**
 * The server orders lifecycle rows (`seq=0`) before every tool row; the panel
 * re-sorts by `(ts, seq, event_id)` so a turn reads as a timeline.
 * @param a
 * @param b
 */
export function compareEvents(a: TrajectoryEvent, b: TrajectoryEvent): number {
  return a.ts.localeCompare(b.ts) || a.seq - b.seq || a.event_id.localeCompare(b.event_id);
}

/**
 * Group events by turn, oldest turn first, each turn sorted chronologically.
 * @param events Raw ledger rows.
 */
export function groupTurns(events: TrajectoryEvent[]): TrajectoryTurn[] {
  const byTurn = new Map<string, TrajectoryEvent[]>();
  for (const event of events) {
    const turnId = event.turn_id || 'unknown';
    const list = byTurn.get(turnId);
    if (list) list.push(event);
    else byTurn.set(turnId, [event]);
  }
  const turns = [...byTurn.entries()].map(([turnId, rows]) => ({
    turnId,
    events: rows.sort(compareEvents)
  }));
  turns.sort((a, b) => compareEvents(a.events[0]!, b.events[0]!));
  return turns;
}

/**
 * Header totals across the given turns.
 * @param turns
 */
export function summarizeTurns(turns: TrajectoryTurn[]): TrajectorySummary {
  let eventCount = 0;
  let toolCount = 0;
  let totalToolMs = 0;
  for (const turn of turns) {
    for (const event of turn.events) {
      eventCount += 1;
      if (event.kind !== 'tool_start') continue;
      toolCount += 1;
      const duration = event.payload?.duration_ms;
      if (typeof duration === 'number' && Number.isFinite(duration)) totalToolMs += duration;
    }
  }
  return { eventCount, toolCount, totalToolMs };
}

/**
 * Human duration: `820ms`, `1.24s`, `12.3s` (ZCode's format).
 * @param ms Milliseconds.
 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(2)}s`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Local clock time for a ledger timestamp (`HH:MM:SS`).
 * @param ts Ledger timestamp.
 */
export function formatClock(ts: string): string {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleTimeString();
}

/**
 * JSON with the given indentation, falling back to String for odd payloads.
 * @param value
 */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * Compact one-line JSON (whitespace removed) capped at `limit` chars.
 * @param value
 * @param limit
 */
export function compactJson(value: unknown, limit = 120): string {
  let text: string;
  try {
    text = JSON.stringify(value) ?? '';
  } catch {
    text = String(value);
  }
  text = text.replace(/\s+/g, ' ');
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/**
 * Closed-row preview for one event, derived from its kind + payload.
 * @param event
 */
export function previewOf(event: TrajectoryEvent): string {
  const payload = event.payload ?? {};
  switch (event.kind) {
    case 'tool_start': {
      const name = String(payload.tool_name ?? event.summary ?? 'tool');
      const args = payload.args;
      return args && typeof args === 'object' ? `${name} ${compactJson(args)}` : name;
    }
    case 'compact': {
      const phase = String(payload.phase ?? '—');
      const reason = String(payload.reason ?? '—');
      return `${phase}/${reason}, ${String(payload.messages_before ?? '?')}→${String(payload.messages_after ?? '?')}`;
    }
    case 'model_retry':
      return `${String(payload.attempt ?? '?')}/${String(payload.max_attempts ?? '?')} · ${String(
        payload.status ?? '—'
      )} · ${String(payload.reason ?? '—')} · ${String(payload.delay_ms ?? 0)}ms`;
    case 'invalid_tool_call':
      return `${String(payload.tool_name ?? 'tool')} · ${
        payload.repaired ? 'repaired' : 'unrepaired'
      }${payload.error ? ` · ${String(payload.error)}` : ''}`;
    case 'turn_start':
      return String(payload.source ?? event.summary ?? 'turn started');
    case 'turn_end':
      return `total ${formatDuration(Number(payload.total_ms ?? 0))} · ttfb ${
        payload.ttfb_ms == null ? '—' : formatDuration(Number(payload.ttfb_ms))
      }`;
    default:
      return event.summary || event.kind;
  }
}

/**
 * Whether an event matches a case-insensitive query over its summary and
 * payload (the filter-style search).
 * @param event
 * @param query
 */
export function eventMatches(event: TrajectoryEvent, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystack = `${event.summary} ${event.kind} ${JSON.stringify(event.payload ?? {})}`.toLowerCase();
  return haystack.includes(needle);
}
