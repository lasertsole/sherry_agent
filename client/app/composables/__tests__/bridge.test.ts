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

describe('sendChatMessage (browser WebSocket)', () => {
  it('opens the WS URL, sends the request, and streams text chunks', async () => {
    // `image_base64_list` triggers the async `/images/upload` path before the
    // WebSocket is created, so stub `fetch` and wait for the upload to resolve
    // before grabbing the WS instance.
    const uploadFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, url: 'http://localhost:8080/uploads/img1.png' }),
    });
    vi.stubGlobal('fetch', uploadFetch);

    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage(
      { session_id: 's1', text: 'hi', image_base64_list: ['img1'] },
      onChunk,
    );

    // Let the upload (`fetch` + `resp.json`) resolve so the WebSocket is created.
    await vi.waitFor(() => expect(FakeWebSocket.instances[0]).toBeTruthy());

    const ws = FakeWebSocket.instances[0];
    expect(ws).toBeTruthy();
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');

    ws.open();
    // base64 payload is uploaded and its resolved URL sent as image_path_list.
    expect(ws.sent).toEqual([
      JSON.stringify({
        session_id: 's1',
        multi_modal_message: {
          text: 'hi',
          image_base64_list: [],
          image_path_list: ['http://localhost:8080/uploads/img1.png'],
        },
      }),
    ]);

    ws.frame({ event: 'chunk', session_id: 's1', content: 'hel', type: 'text' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'lo', type: 'text' });
    expect(onChunk).toHaveBeenNthCalledWith(1, 'hel', 'text');
    expect(onChunk).toHaveBeenNthCalledWith(2, 'lo', 'text');

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
    expect(ws.closed).toBe(true);
  });

  it('defaults text and images when not provided', async () => {
    const promise = bridge.sendChatMessage({ session_id: 's9' }, () => {});
    const ws = FakeWebSocket.instances[0];
    ws.open();
    expect(ws.sent[0]).toBe(
      JSON.stringify({
        session_id: 's9',
        multi_modal_message: { text: '', image_base64_list: [], image_path_list: [] },
      }),
    );
    ws.frame({ event: 'done', session_id: 's9', content: '' });
    await promise;
  });

  it('rejects on error frame', async () => {
    const promise = bridge.sendChatMessage({ session_id: 's1' }, () => {});
    const ws = FakeWebSocket.instances[0];
    ws.open();
    ws.frame({ event: 'error', session_id: 's1', content: 'boom' });
    await expect(promise).rejects.toThrow('boom');
    expect(ws.closed).toBe(true);
  });

  it('rejects on socket error before done', async () => {
    const promise = bridge.sendChatMessage({ session_id: 's1' }, () => {});
    const ws = FakeWebSocket.instances[0];
    ws.error();
    await expect(promise).rejects.toThrow('WebSocket connection error');
    expect(ws.closed).toBe(true);
  });

  it('rejects when the socket closes before completion', async () => {
    const promise = bridge.sendChatMessage({ session_id: 's1' }, () => {});
    const ws = FakeWebSocket.instances[0];
    ws.closeFromServer();
    await expect(promise).rejects.toThrow('WebSocket closed before stream completion');
  });
});

describe('stopChatMessage (browser WebSocket)', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  it('connects to the WS url and sends a stop frame on open', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');

    // Server acknowledges the stop -> resolves
    ws.onopen?.({});
    expect(ws.sent).toEqual([JSON.stringify({ type: 'stop', session_id: 'abc' })]);

    ws.onmessage?.({ data: JSON.stringify({ event: 'stopped', session_id: 'abc' }) });
    await promise;
    expect(ws.closed).toBe(true);
  });

  it('rejects when a different session id is stopped', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.({});
    ws.onmessage?.({ data: JSON.stringify({ event: 'stopped', session_id: 'other' }) });

    // Not the matching session - should stay pending and not close.
    let settled = false;
    promise.then(
      () => (settled = true),
      () => (settled = true),
    );
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(ws.closed).toBe(false);
  });

  it('rejects on WebSocket error', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0];
    ws.onerror?.({});
    await expect(promise).rejects.toThrow('WebSocket stop failed');
    expect(ws.closed).toBe(true);
  });

  it('rejects when closed before confirmation', async () => {
    const promise = bridge.stopChatMessage('abc');
    const ws = FakeWebSocket.instances[0];
    ws.onclose?.({});
    await expect(promise).rejects.toThrow('WebSocket closed before stop confirmation');
    expect(ws.closed).toBe(true);
  });
});

describe('session / system prompt / character / health (browser, via fetchApi)', () => {
  it('clearSession calls DELETE /sessions via fetchApi', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.clearSession('abc');
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/sessions',
      opts: { session_id: 'abc' },
      method: 'delete',
    });
  });

  it('getHistory calls GET /n_turns_history_messages', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200, data: [] });
    await bridge.getHistory('s1', 5);
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/n_turns_history_messages',
      opts: { session_id: 's1', last_turn_count: 5 },
      method: 'get',
    });
  });

  it('readSystemPrompt calls GET /system_prompt and returns the Response', async () => {
    const resp = { code: 200, data: { a: 'b' } };
    mocks.fetchApi.mockResolvedValue(resp);
    await expect(bridge.readSystemPrompt()).resolves.toEqual(resp);
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/system_prompt',
      method: 'get',
    });
  });

  it('writeSystemPrompt calls PUT /system_prompt', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.writeSystemPrompt({ 'ID.md': 'x' });
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'ID.md': 'x' } },
      method: 'put',
    });
  });

  it('updateSystemPrompt calls PUT /system_prompt with merge payload', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.updateSystemPrompt({ 'SOUL.md': 'y' });
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/system_prompt',
      opts: { file_to_content: { 'SOUL.md': 'y' } },
      method: 'put',
    });
  });

  it('readCharacter calls GET /character and returns the Response', async () => {
    const resp = { code: 200, data: { name: { value: 'S' } } };
    mocks.fetchApi.mockResolvedValue(resp);
    await expect(bridge.readCharacter()).resolves.toEqual(resp);
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/character',
      method: 'get',
    });
  });

  it('writeCharacter calls PUT /character', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.writeCharacter({ name: { value: 'S' } });
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/character',
      opts: { character_data: { name: { value: 'S' } } },
      method: 'put',
    });
  });

  it('updateCharacter calls PUT /character with merge payload', async () => {
    mocks.fetchApi.mockResolvedValue({ code: 200 });
    await bridge.updateCharacter({ trait: { value: 'cold' } });
    expect(mocks.fetchApi).toHaveBeenCalledWith({
      url: '/character',
      opts: { character_data: { trait: { value: 'cold' } } },
      method: 'put',
    });
  });

  it('checkHealth returns healthy when the fetch succeeds', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: true });
    await expect(bridge.checkHealth()).resolves.toEqual({
      healthy: true,
      message: 'Python backend reachable',
    });
  });

  it('checkHealth returns unhealthy on HTTP error', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });
    await expect(bridge.checkHealth()).resolves.toEqual({
      healthy: false,
      message: 'HTTP 503',
    });
  });

  it('checkHealth returns unhealthy when fetch throws', async () => {
    (globalThis as any).fetch = vi.fn().mockRejectedValue(new Error('net down'));
    const result = await bridge.checkHealth();
    expect(result.healthy).toBe(false);
    expect(result.message).toContain('net down');
  });
});
