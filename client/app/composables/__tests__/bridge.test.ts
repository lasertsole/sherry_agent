import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// bridge.ts *explicitly imports* fetchApi from './requestApi' (not a Nuxt
// auto-import), so we mock that module directly.
const mocks = vi.hoisted(() => ({
  fetchApi: vi.fn()
}));

vi.mock('../requestApi', () => ({
  fetchApi: mocks.fetchApi
}));

import * as bridge from '../bridge';

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
  /**
   * Simulate an inbound frame (assumes string payload).
   * @param payload
   */
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
  // Each connection now lives in a per-session singleton registry: drop any
  // socket a previous test left alive so this test gets a fresh connection.
  bridge.closeAllAgentSockets();
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

describe('sendChatMessage (browser WebSocket)', () => {
  it('opens the WS URL, sends the request, and streams text chunks', async () => {
    // `image_base64_list` triggers the async `/images/upload` path before the
    // WebSocket is created, so stub `fetch` and wait for the upload to resolve
    // before grabbing the WS instance.
    const uploadFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, url: 'http://localhost:8080/uploads/img1.png' })
    });
    vi.stubGlobal('fetch', uploadFetch);

    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi', image_base64_list: ['img1'] }, onChunk);

    // Let the upload (`fetch` + `resp.json`) resolve so the WebSocket is created.
    await vi.waitFor(() => expect(FakeWebSocket.instances[0]).toBeTruthy());

    const ws = FakeWebSocket.instances[0]!;
    expect(ws).toBeTruthy();
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');

    ws.open();
    // The upload is async; wait until the payload (with the resolved URL) is sent.
    await vi.waitFor(() => expect(ws.sent).toHaveLength(1));
    // base64 payload is uploaded and its resolved URL sent as image_path_list.
    const payload = JSON.parse(ws.sent[0]!);
    expect(payload).toMatchObject({
      session_id: 's1',
      origin: 'user',
      multi_modal_message: {
        text: 'hi',
        image_base64_list: [],
        image_path_list: ['http://localhost:8080/uploads/img1.png'],
        audio_bytes_list: [],
        audio_path_list: [],
        video_bytes_list: [],
        video_path_list: []
      }
    });
    expect(typeof payload.msg_id).toBe('string');

    ws.frame({ event: 'chunk', session_id: 's1', content: 'hel', type: 'text' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'lo', type: 'text' });
    const metaEmpty = { tool_id: undefined, tool_name: undefined, args: undefined, error: undefined };
    expect(onChunk).toHaveBeenNthCalledWith(1, 'hel', 'text', 's1', metaEmpty);
    expect(onChunk).toHaveBeenNthCalledWith(2, 'lo', 'text', 's1', metaEmpty);

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
    // One persistent socket per session: `done` settles the send but keeps it open.
    expect(ws.closed).toBe(false);
  });

  it('streams DeepSeek thinking-mode reasoning chunks and interleaves with text', async () => {
    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi' }, onChunk);

    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');
    ws.open();

    // Simulate a full thinking-mode turn: multi-part reasoning, then text,
    // then a tool call, then a second reasoning pass, then final text.
    ws.frame({ event: 'chunk', session_id: 's1', content: 'Let me think', type: 'reasoning' });
    ws.frame({ event: 'chunk', session_id: 's1', content: ' harder...', type: 'reasoning' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'Sure', type: 'text' });
    ws.frame({
      event: 'chunk',
      session_id: 's1',
      content: '',
      type: 'tool_start',
      tool_id: 't1',
      tool_name: 'web_search'
    });
    // Second thinking pass after the tool result.
    ws.frame({ event: 'chunk', session_id: 's1', content: 'Now I know.', type: 'reasoning' });
    ws.frame({ event: 'chunk', session_id: 's1', content: ' I can answer.', type: 'text' });

    const metaEmpty = { tool_id: undefined, tool_name: undefined, args: undefined, error: undefined };
    expect(onChunk).toHaveBeenNthCalledWith(1, 'Let me think', 'reasoning', 's1', metaEmpty);
    expect(onChunk).toHaveBeenNthCalledWith(2, ' harder...', 'reasoning', 's1', metaEmpty);
    expect(onChunk).toHaveBeenNthCalledWith(3, 'Sure', 'text', 's1', metaEmpty);
    expect(onChunk).toHaveBeenNthCalledWith(4, '', 'tool_start', 's1', {
      tool_id: 't1',
      tool_name: 'web_search',
      args: undefined,
      error: undefined
    });
    expect(onChunk).toHaveBeenNthCalledWith(5, 'Now I know.', 'reasoning', 's1', metaEmpty);
    expect(onChunk).toHaveBeenNthCalledWith(6, ' I can answer.', 'text', 's1', metaEmpty);

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
    expect(ws.closed).toBe(false);
  });

  it('defaults text and images when not provided', async () => {
    const promise = bridge.sendChatMessage({ session_id: 's9', text: '' }, () => {});
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    expect(ws.sent).toHaveLength(1);
    const payload = JSON.parse(ws.sent[0]!);
    expect(payload).toMatchObject({
      session_id: 's9',
      origin: 'user',
      multi_modal_message: {
        text: '',
        image_base64_list: [],
        image_path_list: [],
        audio_bytes_list: [],
        audio_path_list: [],
        video_bytes_list: [],
        video_path_list: []
      }
    });
    expect(typeof payload.msg_id).toBe('string');
    ws.frame({ event: 'done', session_id: 's9', content: '' });
    await promise;
  });

  it('rejects on error frame', async () => {
    const promise = bridge.sendChatMessage({ session_id: 's1', text: '' }, () => {});
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    ws.frame({ event: 'error', session_id: 's1', content: 'boom' });
    await expect(promise).rejects.toThrow('boom');
    // The persistent socket survives an error frame (a later send reuses it).
    expect(ws.closed).toBe(false);
  });

  it('rejects with StreamInterruptedError after exhausting reconnect on socket error before done', async () => {
    vi.useFakeTimers();
    try {
      const promise = bridge.sendChatMessage({ session_id: 's1', text: '' }, () => {});
      // First connection fails -> Case A: enter exponential-backoff reconnect.
      FakeWebSocket.instances[0]!.error();
      expect(FakeWebSocket.instances).toHaveLength(1);
      // Advance each backoff delay and fail the CURRENT (newest) socket: the
      // superseded-socket guard ignores errors from already-replaced sockets.
      await vi.advanceTimersByTimeAsync(1000); // wsReconnectDelayMs(1)
      FakeWebSocket.instances[1]!.error();
      expect(FakeWebSocket.instances).toHaveLength(2);
      await vi.advanceTimersByTimeAsync(2000); // wsReconnectDelayMs(2)
      FakeWebSocket.instances[2]!.error();
      expect(FakeWebSocket.instances).toHaveLength(3);
      await vi.advanceTimersByTimeAsync(4000); // wsReconnectDelayMs(3)
      // After reaching WS_RECONNECT_MAX_ATTEMPTS (3) it still fails => StreamInterruptedError(midStream=false)
      FakeWebSocket.instances[3]!.error();
      await expect(promise).rejects.toMatchObject({
        name: 'StreamInterruptedError',
        midStream: false
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('rejects with StreamInterruptedError after reconnect budget is used when the socket closes before completion', async () => {
    vi.useFakeTimers();
    try {
      const promise = bridge.sendChatMessage({ session_id: 's1', text: '' }, () => {});
      // A pre-chunk disconnect is Case A (no chunk produced yet), so it is safe to retry and resend
      FakeWebSocket.instances[0]!.closeFromServer();
      await vi.advanceTimersByTimeAsync(1000);
      FakeWebSocket.instances[1]!.closeFromServer();
      await vi.advanceTimersByTimeAsync(2000);
      FakeWebSocket.instances[2]!.closeFromServer();
      await vi.advanceTimersByTimeAsync(4000);
      // Reject once retries are exhausted (no longer throws the old 'WebSocket closed before stream completion')
      FakeWebSocket.instances[3]!.closeFromServer();
      await expect(promise).rejects.toMatchObject({ name: 'StreamInterruptedError' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('rejects immediately with midStream error when the socket closes after chunks (never resends)', async () => {
    // Case B: the stream broke after chunks were received — content is already on screen, resending would duplicate it, so it must fail immediately
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi' }, () => {});
    const ws = FakeWebSocket.instances[0]!;
    ws.open();
    ws.frame({ event: 'chunk', session_id: 's1', content: 'partial', type: 'text' });
    ws.closeFromServer();
    await expect(promise).rejects.toMatchObject({
      name: 'StreamInterruptedError',
      midStream: true
    });
    // Never reconnect/resend: still only the original single socket, and the payload was sent exactly once
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(ws.sent).toHaveLength(1);
  });
});

describe('stopChatMessage (persistent session socket)', () => {
  beforeEach(() => {
    bridge.closeAllAgentSockets();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  it('opens the WS url and sends a stop frame on open without closing the socket', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');

    // Server acknowledges the stop -> resolves
    ws.onopen?.({});
    expect(ws.sent).toEqual([JSON.stringify({ type: 'stop', session_id: 'abc' })]);

    ws.onmessage?.({ data: JSON.stringify({ event: 'stopped', session_id: 'abc' }) });
    await promise;
    // Frozen protocol: stop is sent on the SAME socket and does NOT close it.
    expect(ws.closed).toBe(false);
  });

  it('ignores a stopped frame for a different session id', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0]!;
    ws.onopen?.({});
    ws.onmessage?.({ data: JSON.stringify({ event: 'stopped', session_id: 'other' }) });

    // Not the matching session - should stay pending and not close.
    let settled = false;
    promise.then(
      () => (settled = true),
      () => (settled = true)
    );
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(ws.closed).toBe(false);
  });

  it('does not reject on WebSocket error (the persistent socket reconnects instead)', async () => {
    vi.useFakeTimers();
    try {
      const promise = bridge.stopChatMessage('abc');
      const ws = FakeWebSocket.instances[0]!;
      ws.onopen?.({});
      ws.onerror?.({});

      let settled = false;
      promise.then(
        () => (settled = true),
        () => (settled = true)
      );
      await Promise.resolve();
      expect(settled).toBe(false);

      // No pending send to recover -> fallback liveness reconnect after 5s.
      await vi.advanceTimersByTimeAsync(5000);
      expect(FakeWebSocket.instances.length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not reject when the socket closes before confirmation', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0]!;
    ws.onopen?.({});
    ws.closeFromServer();

    let settled = false;
    promise.then(
      () => (settled = true),
      () => (settled = true)
    );
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(ws.readyState).toBe(FakeWebSocket.CLOSED);
  });
});

describe('session / system prompt / character / health (browser, via fetchApi)', () => {
  it('clearSession calls DELETE /sessions via fetchApi', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.clearSession('abc');
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/sessions',
      opts: { session_id: 'abc' },
      method: 'delete'
    });
  });

  it('getHistory calls GET /n_turns_history_messages', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200, data: [] });
    await bridge.getHistory('s1', 5);
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/n_turns_history_messages',
      opts: { session_id: 's1', last_turn_count: 5 },
      method: 'get'
    });
  });

  it('readSystemPrompt calls GET /system_prompt and returns the Response', async () => {
    const resp = { code: 200, data: { a: 'b' } };
    mocks.fetchApi.mockResolvedValue(resp);
    await expect(bridge.readSystemPrompt()).resolves.toEqual(resp);
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/system_prompt',
      method: 'get'
    });
  });

  it('writeSystemPrompt calls PUT /system_prompt', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.writeSystemPrompt({ 'ID.md': 'x' });
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'ID.md': 'x' } },
      method: 'put'
    });
  });

  it('updateSystemPrompt calls PUT /system_prompt with merge payload', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.updateSystemPrompt({ 'SOUL.md': 'y' });
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'SOUL.md': 'y' } },
      method: 'put'
    });
  });

  it('checkHealth returns healthy when the fetch succeeds', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: true });
    await expect(bridge.checkHealth()).resolves.toEqual({
      healthy: true,
      message: 'Python backend reachable'
    });
  });

  it('checkHealth returns unhealthy on HTTP error', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });
    await expect(bridge.checkHealth()).resolves.toEqual({
      healthy: false,
      message: 'HTTP 503'
    });
  });

  it('checkHealth returns unhealthy when fetch throws', async () => {
    (globalThis as any).fetch = vi.fn().mockRejectedValue(new Error('net down'));
    const result = await bridge.checkHealth();
    expect(result.healthy).toBe(false);
    expect(result.message).toContain('net down');
  });
});
