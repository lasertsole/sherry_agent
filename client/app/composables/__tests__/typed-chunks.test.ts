import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

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

describe('sendChatMessage (browser WebSocket) — typed chunks', () => {
  it('passes chunk type to onChunk callback', async () => {
    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi' }, onChunk);

    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    ws.frame({ event: 'chunk', session_id: 's1', content: 'hello', type: 'text' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'web_search', type: 'tool_start' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'web_search', type: 'tool_end' });
    ws.frame({ event: 'chunk', session_id: 's1', content: ' result', type: 'text' });

    expect(onChunk).toHaveBeenNthCalledWith(1, 'hello', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });
    expect(onChunk).toHaveBeenNthCalledWith(2, 'web_search', 'tool_start', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });
    expect(onChunk).toHaveBeenNthCalledWith(3, 'web_search', 'tool_end', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });
    expect(onChunk).toHaveBeenNthCalledWith(4, ' result', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('forwards tool_result meta (tool_id/tool_name/args/error) to onChunk', async () => {
    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi' }, onChunk);

    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    ws.frame({
      event: 'chunk',
      session_id: 's1',
      content: '{"result":"ok"}',
      type: 'tool_result',
      tool_id: 'call_123',
      tool_name: 'web_search',
      args: { query: 'weather' },
      error: false
    });

    expect(onChunk).toHaveBeenCalledWith('{"result":"ok"}', 'tool_result', 's1', {
      tool_id: 'call_123',
      tool_name: 'web_search',
      args: { query: 'weather' },
      error: false
    });

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('forwards tool_result meta with error=true when the tool failed', async () => {
    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi' }, onChunk);

    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    ws.frame({
      event: 'chunk',
      session_id: 's1',
      content: 'timeout',
      type: 'tool_result',
      tool_id: 'call_456',
      tool_name: 'terminal',
      args: { cmd: 'ls' },
      error: true
    });

    expect(onChunk).toHaveBeenCalledWith('timeout', 'tool_result', 's1', {
      tool_id: 'call_456',
      tool_name: 'terminal',
      args: { cmd: 'ls' },
      error: true
    });

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('defaults type to text when not present (backwards compat)', async () => {
    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage({ session_id: 's1', text: 'hi' }, onChunk);

    const ws = FakeWebSocket.instances[0]!;
    ws.open();

    ws.frame({ event: 'chunk', session_id: 's1', content: 'legacy' });
    expect(onChunk).toHaveBeenCalledWith('legacy', 'text', 's1', {
      tool_id: undefined,
      tool_name: undefined,
      args: undefined,
      error: undefined
    });

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('exports AgentChunkType type', () => {
    const types: bridge.AgentChunkType[] = ['text', 'tool_start', 'tool_end', 'tool_result'];
    expect(types).toHaveLength(4);
  });
});
