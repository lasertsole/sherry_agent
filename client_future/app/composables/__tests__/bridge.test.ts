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

function sseResponse(events: string): Response {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(events));
      controller.close();
    },
  });
  return { ok: true, status: 200, body } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.fetchApi.mockReset();
});

describe('sendChatMessage (browser SSE)', () => {
  it('POSTs the request JSON and streams text chunks', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(sseResponse('data: hello\ndata: world\ndata:\n\n'));
    (globalThis as any).fetch = fetchMock;

    const onChunk = vi.fn();
    await bridge.sendChatMessage(
      { session_id: 's1', text: 'hi', image_base64_list: ['img1'] },
      onChunk,
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('http://localhost:8080/sessions/agent/sse');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body)).toEqual({
      session_id: 's1',
      multi_modal_message: {
        text: 'hi',
        image_base64_list: ['img1'],
      },
    });

    expect(onChunk).toHaveBeenNthCalledWith(1, 'hello');
    expect(onChunk).toHaveBeenNthCalledWith(2, 'world');
    // bare "data:" line => empty string chunk
    expect(onChunk).toHaveBeenNthCalledWith(3, '');
  });

  it('defaults text and images when not provided', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(sseResponse('data: hi\n\n'));
    (globalThis as any).fetch = fetchMock;

    await bridge.sendChatMessage({ session_id: 's9' }, () => {});

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({
      session_id: 's9',
      multi_modal_message: { text: '', image_base64_list: [] },
    });
  });

  it('throws when response is not ok', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    await expect(
      bridge.sendChatMessage({ session_id: 's1' }, () => {}),
    ).rejects.toThrow('SSE request failed: HTTP 500');
  });

  it('throws when body is null', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: true, body: null });
    await expect(
      bridge.sendChatMessage({ session_id: 's1' }, () => {}),
    ).rejects.toThrow('response body is not readable');
  });
});

describe('stopChatMessage (browser WebSocket)', () => {
  class FakeWebSocket {
    static instances: FakeWebSocket[] = [];
    onopen: ((ev: any) => void) | null = null;
    onmessage: ((ev: any) => void) | null = null;
    onerror: ((ev: any) => void) | null = null;
    onclose: ((ev: any) => void) | null = null;
    sent: string[] = [];
    url: string;
    closed = false;

    constructor(url: string) {
      this.url = url;
      FakeWebSocket.instances.push(this);
    }
    send(data: string) {
      this.sent.push(data);
    }
    close() {
      this.closed = true;
    }
  }

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
