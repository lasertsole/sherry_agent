import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Queued-frame dispatch tests: the backend reports `{"event":"queued","session_id":"...",
// "position":N,"queue_size":M,"message_id":"..."}` when a message arrives while the session
// is busy (input-queueing-reply-binding). Mirrors the hitl-bridge.test.ts structure.

const mocks = vi.hoisted(() => ({
  fetchApi: vi.fn()
}));

vi.mock('../requestApi', () => ({
  fetchApi: mocks.fetchApi
}));

import * as bridge from '../bridge';
import * as messages from '../messages';

// messages.ts imports streamChatMessage as a direct binding; intercept it via the hoisted
// mutable reference to observe the callbacks postAgentStream hands to it.
type StreamImpl = (
  request: bridge.ChatRequest,
  onChunk: bridge.OnChunkCallback,
  onHitl?: bridge.OnHitlCallback,
  onDone?: bridge.OnDoneCallback,
  onQueued?: bridge.OnQueuedCallback
) => { controller: bridge.StreamController; promise: Promise<void> };

const mutable = vi.hoisted(() => ({
  streamImpl: undefined as StreamImpl | undefined
}));

vi.mock('../bridge', async importOriginal => {
  const actual = await importOriginal<typeof bridge>();
  return {
    ...actual,
    streamChatMessage: (...args: Parameters<typeof bridge.streamChatMessage>) => {
      if (mutable.streamImpl) return mutable.streamImpl(...args);
      return actual.streamChatMessage(...args);
    }
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.fetchApi.mockReset();
});

/** Minimal WebSocket double used by the browser-mode tests. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  onopen: ((ev: any) => void) | null = null;
  onmessage: ((ev: any) => void) | null = null;
  onerror: ((ev: any) => void) | null = null;
  onclose: ((ev: any) => void) | null = null;
  sent: string[] = [];
  url: string;
  closed = false;
  readyState = FakeWebSocket.CONNECTING;

  constructor(url: string) {
    this.url = url;
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

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

async function awaitSocket(): Promise<FakeWebSocket> {
  await vi.waitFor(() => expect(FakeWebSocket.instances[0]).toBeTruthy());
  const ws = FakeWebSocket.instances[0]!;
  ws.open();
  return ws;
}

/** Plan-fixed queued frame contract (backend Task 7 implements the same shape). */
function queuedFrame(sessionId = 's1', position = 2, queueSize = 5): object {
  return { event: 'queued', session_id: sessionId, position, queue_size: queueSize, message_id: 'msg-42' };
}

describe('streamChatMessage queued dispatch (onQueued)', () => {
  it('invokes onQueued with camelCase fields on a queued frame', async () => {
    const onQueued = vi.fn();
    const { promise } = bridge.streamChatMessage({ session_id: 's1', text: 'hi' }, vi.fn(), undefined, undefined, onQueued);
    const ws = await awaitSocket();

    ws.frame(queuedFrame('s1', 2, 5));

    expect(onQueued).toHaveBeenCalledTimes(1);
    expect(onQueued).toHaveBeenCalledWith({
      sessionId: 's1',
      position: 2,
      queueSize: 5,
      messageId: 'msg-42'
    });
    void promise;
  });

  it('falls back to the request session id when the frame omits session_id', async () => {
    const onQueued = vi.fn();
    const { promise } = bridge.streamChatMessage({ session_id: 's1', text: 'hi' }, vi.fn(), undefined, undefined, onQueued);
    const ws = await awaitSocket();

    ws.frame({ event: 'queued', position: 1, queue_size: 1 });

    expect(onQueued).toHaveBeenCalledWith({ sessionId: 's1', position: 1, queueSize: 1, messageId: undefined });
    void promise;
  });

  it('silently ignores queued frames when no onQueued is supplied', async () => {
    const { promise } = bridge.streamChatMessage({ session_id: 's1', text: 'hi' }, vi.fn());
    const ws = await awaitSocket();

    expect(() => ws.frame(queuedFrame())).not.toThrow();
    void promise;
  });

  it('does not consume the stream: queued frame then chunk then done resolves normally', async () => {
    const onChunk = vi.fn();
    const onQueued = vi.fn();
    const { promise } = bridge.streamChatMessage({ session_id: 's1', text: 'hi' }, onChunk, undefined, undefined, onQueued);
    const ws = await awaitSocket();

    ws.frame(queuedFrame('s1', 1, 3));
    ws.frame({ event: 'chunk', session_id: 's1', content: 'hello', type: 'text' });
    ws.frame({ event: 'done', session_id: 's1' });

    await expect(promise).resolves.toBeUndefined();
    expect(onQueued).toHaveBeenCalledTimes(1);
    expect(onChunk).toHaveBeenCalledWith('hello', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });
  });
});

describe('postAgentStream onQueued passthrough (messages.ts)', () => {
  it('threads onQueued through to the underlying stream', () => {
    const onQueuedSpy = vi.fn();
    let captured: bridge.OnQueuedCallback | undefined;

    mutable.streamImpl = (_req, _onChunk, _onHitl, _onDone, onQueued) => {
      captured = onQueued;
      const controller: bridge.StreamController = { closed: false, abort: vi.fn() };
      return { controller, promise: Promise.resolve() };
    };
    try {
      messages.postAgentStream('s1', { text: 'hi' }, vi.fn(), undefined, undefined, undefined, onQueuedSpy);

      // onQueued propagated into streamChatMessage and reaches the consumer when invoked.
      expect(captured).toBe(onQueuedSpy);
      captured?.({ sessionId: 's1', position: 3, queueSize: 7 });
      expect(onQueuedSpy).toHaveBeenCalledWith({ sessionId: 's1', position: 3, queueSize: 7 });
    } finally {
      mutable.streamImpl = undefined;
    }
  });
});
