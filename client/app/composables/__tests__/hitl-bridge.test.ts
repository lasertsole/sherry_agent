import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// bridge.ts *explicitly imports* fetchApi from './requestApi' (not a Nuxt
// auto-import), so we mock that module directly.
const mocks = vi.hoisted(() => ({
  fetchApi: vi.fn(),
}));

vi.mock('../requestApi', () => ({
  fetchApi: mocks.fetchApi,
}));

import * as bridge from '../bridge';
import * as messages from '../messages';

// `messages.ts` imports `streamChatMessage` as a direct binding, so to observe
// the HITL callbacks handed to it we mock `../bridge` and keep a controllable
// reference (`mutable.streamImpl`). Browser WebSocket tests below use the *real*
// `streamChatMessage` implementation (delegated through), while the
// `postAgentStream` test temporarily stubs it via `mutable.streamImpl`.
type StreamImpl = (
  request: { session_id: string; text: string },
  onChunk: bridge.OnChunkCallback,
  onHitl?: bridge.OnHitlCallback,
) => { controller: bridge.StreamController; promise: Promise<void> };

const mutable = vi.hoisted(() => ({
  streamImpl: undefined as StreamImpl | undefined,
}));

vi.mock('../bridge', async (importOriginal) => {
  const actual = await importOriginal<typeof bridge>();
  return {
    ...actual,
    streamChatMessage: (...args: Parameters<typeof bridge.streamChatMessage>) => {
      if (mutable.streamImpl) return mutable.streamImpl(...args);
      return actual.streamChatMessage(...args);
    },
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
  /** Simulate the backend opening the connection. */
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }
  /** Simulate an inbound frame (assumes string payload). */
  frame(payload: unknown) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
    this.onmessage?.({ data });
  }
  error() {
    this.onerror?.({});
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

// The HITL flow intentionally never uploads images, so no `fetch` stub is
// needed before the WebSocket is opened: `sendChatMessageWs` creates the
// socket synchronously when `image_base64_list` is absent.

function openStream(
  text = 'hi',
  sessionId = 's1',
): { controller: bridge.StreamController; promise: Promise<void> } {
  return bridge.streamChatMessage(
    { session_id: sessionId, text },
    vi.fn(),
  );
}

async function awaitSocket(): Promise<FakeWebSocket> {
  await vi.waitFor(() => expect(FakeWebSocket.instances[0]).toBeTruthy());
  const ws = FakeWebSocket.instances[0];
  ws.open();
  return ws;
}

// Convenience: build a realistic HITL interrupt payload. The bridge's onmessage
// handler passes `data.content` straight to `onHitl`, so `content` is the object
// (HitlInterruptData), not a double-encoded string.
function hitlInterruptFrame(): string {
  return JSON.stringify({
    event: 'hitl_request',
    session_id: 's1',
    content: {
      tool_name: 'terminal',
      tool_args: { command: 'rm -rf data' },
      description: '执行危险命令需要人工确认',
      allowed_decisions: ['approve', 'reject'],
    },
  });
}

describe('sendHitlResponse (browser WebSocket)', () => {
  it('opens the WS to /sessions/agent/ws without images', async () => {
    const { promise } = openStream();
    const ws = await awaitSocket();
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');
    // Initial send is the chat message payload with an empty image URL list.
    expect(ws.sent[0]).toContain('"session_id":"s1"');
    void promise;
  });

  it('sends an approve decision frame on the open socket', async () => {
    const { controller, promise } = openStream();
    const ws = await awaitSocket();

    controller.sendHitlResponse?.({ decision: 'approve' });

    expect(ws.sent).toHaveLength(2);
    const frame = JSON.parse(ws.sent[1]);
    expect(frame).toMatchObject({
      type: 'hitl_response',
      session_id: 's1',
      decision: 'approve',
    });
    expect(frame.message).toBe('');
    void promise;
  });

  it('sends a reject decision with an optional message', async () => {
    const { controller, promise } = openStream();
    const ws = await awaitSocket();

    controller.sendHitlResponse?.({ decision: 'reject', message: '驳回，命令太危险' });

    const frame = JSON.parse(ws.sent[1]);
    expect(frame.type).toBe('hitl_response');
    expect(frame.decision).toBe('reject');
    expect(frame.message).toBe('驳回，命令太危险');
    void promise;
  });

  it('sends an edit decision together with edited_args', async () => {
    const { controller, promise } = openStream();
    const ws = await awaitSocket();

    controller.sendHitlResponse?.({
      decision: 'edit',
      message: '让我改成安全命令',
      edited_args: { command: 'ls -la data' },
    });

    const frame = JSON.parse(ws.sent[1]);
    expect(frame.type).toBe('hitl_response');
    expect(frame.decision).toBe('edit');
    expect(frame.edited_args).toEqual({ command: 'ls -la data' });
    expect(frame.message).toBe('让我改成安全命令');
    void promise;
  });
});

describe('streamChatMessage HITL dispatch (onHitl)', () => {
  it('invokes onHitl with the parsed HitlInterruptData on hitl_request', async () => {
    const onHitl = vi.fn();
    const { promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'hi' },
      vi.fn(),
      onHitl,
    );
    const ws = await awaitSocket();

    ws.frame(JSON.parse(hitlInterruptFrame()));

    const data = onHitl.mock.calls[0][0];
    expect(data).toEqual(JSON.parse(hitlInterruptFrame()).content);
    expect(data.tool_name).toBe('terminal');
    expect(data.tool_args).toEqual({ command: 'rm -rf data' });
    expect(data.allowed_decisions).toEqual(['approve', 'reject']);
    void promise;
  });

  it('routes chunk frames to onChunk with parsed content and type', async () => {
    const onChunk = vi.fn();
    const { promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
      vi.fn(),
    );
    const ws = await awaitSocket();

    ws.frame({ event: 'chunk', session_id: 's1', content: '你好', type: 'text' });
    expect(onChunk).toHaveBeenCalledWith('你好', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined,
    });

    ws.frame({ event: 'chunk', session_id: 's1', content: '' });
    // Missing `type` defaults to 'text'; empty content is still delivered.
    expect(onChunk).toHaveBeenCalledWith('', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined,
    });
    expect(onChunk).toHaveBeenCalledTimes(2);
    void promise;
  });

  it('resolves the stream promise when the server sends done', async () => {
    const onChunk = vi.fn();
    const { promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
      vi.fn(),
    );
    const ws = await awaitSocket();

    ws.frame({ event: 'chunk', session_id: 's1', content: 'final', type: 'text' });
    ws.frame({ event: 'done', session_id: 's1' });

    await expect(promise).resolves.toBeUndefined();
    expect(onChunk).toHaveBeenCalledWith('final', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined,
    });
  });
});

describe('postAgentStream HITL passthrough (messages.ts)', () => {
  it('passes onHitl through to the underlying stream and mounts sendHitlResponse', () => {
    const onHitlSpy = vi.fn();
    const captured = {
      response: undefined as bridge.HitlResponse | undefined,
      onHitl: undefined as bridge.OnHitlCallback | undefined,
    };

    // Stub streamChatMessage via the hoisted mutable so the direct-binding
    // import inside messages.ts is intercepted.
    mutable.streamImpl = (_req, _onChunk, onHitl) => {
      captured.onHitl = onHitl;
      const controller: bridge.StreamController = {
        closed: false,
        abort: vi.fn(),
        sendHitlResponse: (response) => {
          captured.response = response;
        },
      };
      return { controller, promise: Promise.resolve() };
    };
    try {
      const controller = messages.postAgentStream(
        's1',
        { text: 'hi' },
        vi.fn(),
        undefined,
        undefined,
        onHitlSpy,
      );

      // onHitl propagated into streamChatMessage.
      expect(captured.onHitl).toBe(onHitlSpy);

      // trigger the HITL callback and ensure it reaches the consumer.
      captured.onHitl?.({
        tool_name: 'terminal',
        tool_args: { command: 'rm -rf data' },
        description: '需人工审批',
        allowed_decisions: ['approve', 'reject'],
      });
      expect(onHitlSpy).toHaveBeenCalledTimes(1);

      // The consumer-facing AbortController carries sendHitlResponse.
      const wired = (controller as unknown as {
        sendHitlResponse?: (r: bridge.HitlResponse) => void;
      }).sendHitlResponse;
      expect(typeof wired).toBe('function');
      wired?.({ decision: 'approve' });
      expect(captured.response).toEqual({ decision: 'approve' });
    } finally {
      mutable.streamImpl = undefined;
    }
  });
});

describe('HITL resilience (edge cases)', () => {
  it('silently ignores hitl_request when no onHitl is supplied', async () => {
    // Default openStream passes no onHitl.
    const { promise } = openStream();
    const ws = await awaitSocket();

    // Should not throw even with a realistic backend payload.
    expect(() => ws.frame(JSON.parse(hitlInterruptFrame()))).not.toThrow();
    void promise;
  });

  it('silently ignores hitl_request with empty content', async () => {
    const onChunk = vi.fn();
    const onHitl = vi.fn();
    const { promise } = bridge.streamChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
      onHitl,
    );
    const ws = await awaitSocket();

    ws.frame({ event: 'hitl_request', session_id: 's1', content: '' });
    expect(onHitl).not.toHaveBeenCalled();
    expect(onChunk).not.toHaveBeenCalled();
    void promise;
  });

  it('does not send hitl_response after the stream is done/aborted', async () => {
    const { controller, promise } = openStream();
    const ws = await awaitSocket();

    // abort() marks stream done, sends a stop frame, and closes the socket.
    controller.abort();
    controller.sendHitlResponse?.({ decision: 'approve' });
    // Only the initial chat payload + the stop frame are sent; no hitl_response.
    expect(ws.sent).toEqual([
      JSON.stringify({
      session_id: 's1',
      multi_modal_message: {
        text: 'hi',
        image_base64_list: [],
        image_path_list: [],
        audio_bytes_list: [],
        audio_path_list: [],
        video_bytes_list: [],
        video_path_list: [],
      },
      }),
      JSON.stringify({ type: 'stop', session_id: 's1' }),
    ]);
    // User-initiated abort does NOT reject the stream promise (the release
    // guard early-returns on the already-done flag), so it stays pending.
    expect(controller.closed).toBe(true);
    void promise;
  });

  it('does not send hitl_response when the WebSocket is not OPEN', async () => {
    const { controller, promise } = openStream();
    const ws = await awaitSocket();

    // Server closes the socket (e.g. reconnect desired) → readyState CLOSED.
    ws.closeFromServer();
    controller.sendHitlResponse?.({ decision: 'reject', message: '连接已断开' });
    // No hitl_response frame appended after the close.
    expect(ws.sent.every((f) => !f.includes('hitl_response'))).toBe(true);
    await expect(promise).rejects.toThrow();
  });
});

describe('HITL trigger patterns match the backend guardrails', () => {
  it('hitl_response approve includes all expected fields for resume_agent', async () => {
    // This lock documents the exact contract consumed by
    // `server/trigger/ws/messages.py → resume_agent`.
    const { controller, promise } = openStream();
    const ws = await awaitSocket();

    controller.sendHitlResponse?.({
      decision: 'approve',
      message: 'gogo',
      edited_args: { command: 'ls' },
    });

    const frame = JSON.parse(ws.sent[1]);
    expect(frame).toEqual({
      type: 'hitl_response',
      session_id: 's1',
      decision: 'approve',
      message: 'gogo',
      edited_args: { command: 'ls' },
    });
    void promise;
  });
});
