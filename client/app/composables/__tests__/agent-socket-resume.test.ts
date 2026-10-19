/**
 * Stream-resume behaviour of the per-session agent socket.
 *
 * Pinned contract (no duplicate content, ever):
 *  - a mid-stream connection loss does NOT reject the send and does NOT
 *    resend the generation payload;
 *  - on reconnect the PAGE fetches history + the active-turn snapshot in one
 *    HTTP request; the socket only holds inbound frames while that request is
 *    in flight and flushes them after the snapshot watermark is applied;
 *  - the legacy `stream_resume` reply is still accepted (`ended` settles the
 *    send like `done`); an exhausted retry budget rejects it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchApi: vi.fn() }));

vi.mock('../requestApi', () => ({
  fetchApi: mocks.fetchApi,
  ensureGatewayToken: async () => null,
  withGatewayToken: (url: string) => url
}));

import { acquireAgentSocket, closeAllAgentSockets } from '../bridge/agent-socket';
import { on, off } from '../mitt';

/** Minimal WebSocket double (same shape as the HITL-bridge harness). */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  sent: string[] = [];
  closed = false;
  readyState = FakeWebSocket.CONNECTING;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }
  frame(payload: unknown) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
    this.onmessage?.({ data });
  }
  closeFromServer() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({});
  }
}

const generation = (msgId = 'm1') => ({ text: 'hi', msg_id: msgId }) as never;

beforeEach(() => {
  closeAllAgentSockets();
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('agent socket stream resume', () => {
  it('keeps a mid-stream send pending, reconnects, and never sends resume_stream', () => {
    const socket = acquireAgentSocket('s1');
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    const { controller } = socket.send(generation());
    ws.frame({ event: 'turn_started', turn_id: 't1', message_ids: ['m1'] });
    ws.frame({ event: 'chunk', content: 'abc', type: 'text' });

    // Mid-stream loss: the send must NOT be rejected (the turn keeps running).
    ws.closeFromServer();
    expect(controller.closed).toBe(false);

    const reconnected: unknown[] = [];
    const listener = (payload: unknown) => reconnected.push(payload);
    on('stream:reconnected', listener);
    let resumed: FakeWebSocket;
    try {
      vi.advanceTimersByTime(1000);
      resumed = FakeWebSocket.instances[1]!;
      resumed.open();
      expect(reconnected).toEqual([{ sessionId: 's1' }]);
    } finally {
      off('stream:reconnected', listener);
    }

    const frames = resumed!.sent.map(raw => JSON.parse(raw));
    expect(frames.some(frame => frame.type === 'resume_stream')).toBe(false);
    // The generation payload itself is NEVER re-sent (that would replay the turn).
    expect(frames.some(frame => frame.text === 'hi')).toBe(false);
  });

  it('holds inbound frames until release (history+snapshot in flight)', () => {
    const socket = acquireAgentSocket('s1');
    const onChunk = vi.fn();
    socket.setHandlers({ onChunk });
    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    socket.holdFrames();
    ws.frame({ event: 'chunk', content: 'held', type: 'text' });
    expect(onChunk).not.toHaveBeenCalled();

    socket.setSnapshotWatermark(0, 't1');
    socket.releaseFrames();
    expect(onChunk).toHaveBeenCalledTimes(1);
    expect(onChunk.mock.calls[0]![0]).toBe('held');
  });

  it('caps the held-frame queue at 1000 (oldest dropped)', () => {
    const socket = acquireAgentSocket('s1');
    const onChunk = vi.fn();
    socket.setHandlers({ onChunk });
    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    socket.holdFrames();
    for (let i = 0; i < 1005; i++) {
      ws.frame({ event: 'chunk', content: `c${i}`, type: 'text', turn_id: 't1' });
    }
    socket.releaseFrames();
    expect(onChunk).toHaveBeenCalledTimes(1000);
    expect(onChunk.mock.calls[0]![0]).toBe('c5');
  });

  it('accepts a legacy snapshot reply as a replacement value', () => {
    const emitted: unknown[] = [];
    const listener = (payload: unknown) => emitted.push(payload);
    on('stream:resumed', listener);

    try {
      const socket = acquireAgentSocket('s1');
      const ws = FakeWebSocket.instances[0]!;
      ws.open();
      socket.send(generation());
      ws.frame({ event: 'turn_started', turn_id: 't1', message_ids: ['m1'] });
      ws.frame({ event: 'chunk', content: 'abc', type: 'text' });
      ws.frame({
        event: 'stream_resume',
        session_id: 's1',
        content: { status: 'ok', text: 'abcdef', seq: 3, turn_id: 't1' }
      });

      expect(emitted.at(-1)).toEqual({
        sessionId: 's1',
        status: 'ok',
        text: 'abcdef',
        hasReasoning: false,
        seq: 3,
        turnId: 't1',
        turnNum: undefined
      });
      // The reply carries a TEXT value — nothing the client could replay.
      expect(emitted.at(-1)).not.toHaveProperty('frames');
    } finally {
      off('stream:resumed', listener);
    }
  });

  it('settles the pending send when the turn already ended while disconnected', async () => {
    const socket = acquireAgentSocket('s1');
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    const { controller, promise } = socket.send(generation());
    ws.frame({ event: 'turn_started', turn_id: 't1', message_ids: ['m1'] });
    ws.frame({ event: 'chunk', content: 'abc', type: 'text' });
    ws.closeFromServer();
    vi.advanceTimersByTime(1000);
    const resumed = FakeWebSocket.instances[1]!;
    resumed.open();
    resumed.frame({ event: 'stream_resume', session_id: 's1', content: { status: 'ended' } });

    await expect(promise).resolves.toBeUndefined();
    expect(controller.closed).toBe(true);
  });

  it('rejects a mid-stream send once the reconnect budget is exhausted', async () => {
    const socket = acquireAgentSocket('s1');
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    const { promise } = socket.send(generation());
    ws.frame({ event: 'turn_started', turn_id: 't1', message_ids: ['m1'] });
    ws.frame({ event: 'chunk', content: 'abc', type: 'text' });

    // The budget counts CONSECUTIVE failed connects (a successful open resets
    // it), so every retry fails without opening: initial loss, then three
    // failed reconnect attempts.
    ws.closeFromServer();
    for (const delay of [1000, 2000, 4000]) {
      vi.advanceTimersByTime(delay);
      FakeWebSocket.instances.at(-1)!.closeFromServer();
    }

    await expect(promise).rejects.toThrow();
  });
});
