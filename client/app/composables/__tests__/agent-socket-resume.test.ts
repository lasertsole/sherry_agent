/**
 * Stream-resume behaviour of the per-session agent socket.
 *
 * Pinned contract (no duplicate content, ever):
 *  - a mid-stream connection loss does NOT reject the send and does NOT
 *    resend the generation payload;
 *  - on reconnect the socket asks for a text snapshot (`resume_stream`) and the
 *    reply only updates the view — frames are never replayed;
 *  - `ended` settles the send like `done`; an exhausted retry budget rejects it.
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
  it('keeps a mid-stream send pending and requests a snapshot on reconnect', () => {
    const socket = acquireAgentSocket('s1');
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    const { controller } = socket.send(generation());
    ws.frame({ event: 'turn_started', turn_id: 't1', message_ids: ['m1'] });
    ws.frame({ event: 'chunk', content: 'abc', type: 'text' });

    // Mid-stream loss: the send must NOT be rejected (the turn keeps running).
    ws.closeFromServer();
    expect(controller.closed).toBe(false);

    vi.advanceTimersByTime(1000);
    const resumed = FakeWebSocket.instances[1]!;
    resumed.open();

    const frames = resumed.sent.map(raw => JSON.parse(raw));
    const request = frames.find(frame => frame.type === 'resume_stream');
    expect(request).toMatchObject({ type: 'resume_stream', session_id: 's1' });
    // The generation payload itself is NEVER re-sent (that would replay the turn).
    expect(frames.some(frame => frame.text === 'hi')).toBe(false);
  });

  it('applies the snapshot as a replacement value (never a frame replay)', () => {
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
      ws.closeFromServer();
      vi.advanceTimersByTime(1000);
      const resumed = FakeWebSocket.instances[1]!;
      resumed.open();
      resumed.frame({
        event: 'stream_resume',
        session_id: 's1',
        content: { status: 'ok', text: 'abcdef' }
      });

      expect(emitted.at(-1)).toEqual({
        sessionId: 's1',
        status: 'ok',
        text: 'abcdef',
        hasReasoning: false
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
