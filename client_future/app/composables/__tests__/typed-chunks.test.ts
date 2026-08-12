import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

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
    const promise = bridge.sendChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    ws.frame({ event: 'chunk', session_id: 's1', content: 'hello', type: 'text' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'web_search', type: 'tool_start' });
    ws.frame({ event: 'chunk', session_id: 's1', content: 'web_search', type: 'tool_end' });
    ws.frame({ event: 'chunk', session_id: 's1', content: ' result', type: 'text' });

    expect(onChunk).toHaveBeenNthCalledWith(1, 'hello', 'text');
    expect(onChunk).toHaveBeenNthCalledWith(2, 'web_search', 'tool_start');
    expect(onChunk).toHaveBeenNthCalledWith(3, 'web_search', 'tool_end');
    expect(onChunk).toHaveBeenNthCalledWith(4, ' result', 'text');

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('defaults type to text when not present (backwards compat)', async () => {
    const onChunk = vi.fn();
    const promise = bridge.sendChatMessage(
      { session_id: 's1', text: 'hi' },
      onChunk,
    );

    const ws = FakeWebSocket.instances[0];
    ws.open();

    ws.frame({ event: 'chunk', session_id: 's1', content: 'legacy' });
    expect(onChunk).toHaveBeenCalledWith('legacy', 'text');

    ws.frame({ event: 'done', session_id: 's1', content: '' });
    await promise;
  });

  it('exports AgentChunkType type', () => {
    const types: bridge.AgentChunkType[] = ['text', 'tool_start', 'tool_end'];
    expect(types).toHaveLength(3);
  });
});
