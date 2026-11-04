/**
 * `useTrajectoryStream`: WS wiring, event merging and reconnect behaviour.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { nextTick, ref } from 'vue';
import type { TrajectoryEvent } from '~/composables/bridge/session';

vi.mock('@/composables/requestApi', () => ({
  ensureGatewayToken: vi.fn(async () => null),
  withGatewayToken: (url: string) => url
}));
vi.mock('@/composables/ws-ticket', () => ({
  prepareWsTicket: vi.fn(async () => {}),
  appendWsTicket: (url: string) => url
}));

import { useTrajectoryStream } from '../use-trajectory-stream';

/** A controllable WebSocket double. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.();
  }

  emitOpen(): void {
    this.onopen?.();
  }

  emit(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  drop(): void {
    this.closed = true;
    this.onclose?.();
  }
}

/**
 * A minimal event row.
 * @param overrides
 */
function event(overrides: Partial<TrajectoryEvent> & { event_id: string }): TrajectoryEvent {
  return {
    event_id: overrides.event_id,
    turn_id: 't1',
    seq: 1,
    ts: '2026-11-04T23:00:00.000',
    kind: 'tool_start',
    is_error: false,
    summary: 'read_file',
    payload: {},
    ...overrides
  };
}

describe('useTrajectoryStream', () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('connects to the session channel and tracks the connection state', () => {
    const session = ref<string | null>('child-1');
    const { connected } = useTrajectoryStream(session);

    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0]!.url).toContain('/sessions/trajectory/ws?session_id=child-1');
    expect(connected.value).toBe(false);

    FakeSocket.instances[0]!.emitOpen();
    expect(connected.value).toBe(true);
  });

  it('does not connect without a session', () => {
    const session = ref<string | null>(null);
    const { events, connected } = useTrajectoryStream(session);

    expect(FakeSocket.instances).toHaveLength(0);
    expect(events.value).toEqual([]);
    expect(connected.value).toBe(false);
  });

  it('merges the ready snapshot and each pushed event by event id', () => {
    const session = ref<string | null>('child-1');
    const { events } = useTrajectoryStream(session);
    const socket = FakeSocket.instances[0]!;
    socket.emitOpen();

    socket.emit({
      event: 'ready',
      events: [event({ event_id: 'e1', seq: 1, payload: { args: { path: 'a.py' } } })]
    });
    socket.emit({
      event: 'trajectory_event',
      data: event({ event_id: 'e2', seq: 2, summary: 'terminal', payload: { args: {} } })
    });

    expect(events.value.map(row => row.event_id)).toEqual(['e1', 'e2']);
  });

  it('folds tool_end into its tool_start row and keeps the first seq/ts', () => {
    const session = ref<string | null>('child-1');
    const { events } = useTrajectoryStream(session);
    const socket = FakeSocket.instances[0]!;

    socket.emit({
      event: 'trajectory_event',
      data: event({
        event_id: 'tool-1',
        seq: 2,
        ts: '2026-11-04T23:00:02.000',
        payload: { tool_name: 'read_file', args: { path: 'a.py' } }
      })
    });
    socket.emit({
      event: 'trajectory_event',
      data: event({
        event_id: 'tool-1',
        seq: 3,
        ts: '2026-11-04T23:00:03.000',
        payload: { result: 'body', duration_ms: 12 }
      })
    });

    expect(events.value).toHaveLength(1);
    const row = events.value[0]!;
    expect(row.seq).toBe(2);
    expect(row.ts).toBe('2026-11-04T23:00:02.000');
    expect(row.payload).toEqual({
      tool_name: 'read_file',
      args: { path: 'a.py' },
      result: 'body',
      duration_ms: 12
    });
  });

  it('ignores malformed frames and non-trajectory events', () => {
    const session = ref<string | null>('child-1');
    const { events } = useTrajectoryStream(session);
    const socket = FakeSocket.instances[0]!;

    socket.onmessage?.({ data: 'not json' });
    socket.emit({ event: 'something_else', data: {} });

    expect(events.value).toEqual([]);
  });

  it('reconnects after a dropped socket and gap-fills from the new ready frame', () => {
    vi.useFakeTimers();
    const session = ref<string | null>('child-1');
    const { events, connected } = useTrajectoryStream(session);
    const first = FakeSocket.instances[0]!;
    first.emitOpen();

    first.drop();
    expect(connected.value).toBe(false);
    expect(FakeSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(2100);
    expect(FakeSocket.instances).toHaveLength(2);
    const second = FakeSocket.instances[1]!;
    second.emitOpen();
    second.emit({ event: 'ready', events: [event({ event_id: 'gap' })] });

    expect(connected.value).toBe(true);
    expect(events.value.map(row => row.event_id)).toEqual(['gap']);
  });

  it('resets and reconnects when the session changes', async () => {
    const session = ref<string | null>('child-1');
    const { events } = useTrajectoryStream(session);
    const first = FakeSocket.instances[0]!;
    first.emit({ event: 'ready', events: [event({ event_id: 'old' })] });

    session.value = 'child-2';
    await nextTick();

    expect(first.closed).toBe(true);
    expect(events.value).toEqual([]);
    expect(FakeSocket.instances).toHaveLength(2);
    expect(FakeSocket.instances[1]!.url).toContain('session_id=child-2');
  });
});
