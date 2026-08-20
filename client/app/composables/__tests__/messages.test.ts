import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import type { Response } from '@/types/response';
import type { CachedMessage } from '../db';

// `fetchApi` is used inside messages.ts as a Nuxt auto-import (no explicit
// `import` statement), so it must be stubbed as a global rather than via
// `vi.mock('../requestApi')`.
//
// The Dexie `db` module (`../db`) is mocked so tests never touch a real
// IndexedDB instance.
const dbMock = vi.hoisted(() => ({
  cacheMessages: vi.fn(async () => {}),
  readCachedMessages: vi.fn(async () => []),
  cachedMaxTurnNum: vi.fn(async () => 0),
  clearCachedSession: vi.fn(async () => {}),
}));

vi.mock('../db', () => dbMock);

import {
  get_history_by_turn_page,
  clearSession,
  postAgentStream,
} from '../messages';

function stubFetchApi(data: unknown) {
  const mock = vi.fn().mockResolvedValue(data);
  vi.stubGlobal('fetchApi', mock);
  return mock;
}

let mockDb = dbMock;

beforeEach(() => {
  // Reset in-memory cache state to empty on each test.
  mockDb.readCachedMessages.mockResolvedValue([]);
  mockDb.cachedMaxTurnNum.mockResolvedValue(0);
  mockDb.cacheMessages.mockClear();
  mockDb.clearCachedSession.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('get_history_by_turn_page', () => {
  const rows: CachedMessage[] = [
    { id: 1, turn_num: 1, session_id: 's1', role: 'human', content: 'hi', timestamp: null, tool_call_id: null, tool_calls: null, tool_status: null, tool_name: null, finish_reason: null, reasoning: null, reasoning_content: null, images: null, audios: null, videos: null, model_name: null, input_tokens: null, output_tokens: null },
    { id: 2, turn_num: 2, session_id: 's1', role: 'ai', content: 'hello', timestamp: null, tool_call_id: null, tool_calls: null, tool_status: null, tool_name: null, finish_reason: null, reasoning: null, reasoning_content: null, images: null, audios: null, videos: null, model_name: null, input_tokens: null, output_tokens: null },
  ];

  it('returns cached + fetched merged data (deduped by id)', async () => {
    // Cache already contains turn 1 & 2.
    mockDb.readCachedMessages.mockResolvedValue([rows[0], rows[1]]);
    mockDb.cachedMaxTurnNum.mockResolvedValue(2);
    // Server responds with the missing turn (turn 3).
    const fetched = [{ ...rows[1], id: 3, turn_num: 3, role: 'ai', content: 'world' }];
    const mock = stubFetchApi({ code: 200, data: fetched });

    const result = await get_history_by_turn_page('s1', 0, 10, 1);

    expect(result).toEqual([rows[0], rows[1], fetched[0]]);
    // min_turn_num derives from the cached max turn_num (2), not the caller's 0.
    expect(mock).toHaveBeenCalledWith({
      url: '/get_history_by_turn_page',
      opts: {
        session_id: 's1',
        min_turn_num: 2,
        turn_page_size: 10,
        turn_page_num: 1,
      },
      method: 'get',
    });
    expect(mockDb.cacheMessages).toHaveBeenCalledWith(fetched);
  });

  it('uses caller provided min_turn_num when cache is empty', async () => {
    const data = [{ ...rows[0], id: 10, turn_num: 5 }];
    const mock = stubFetchApi({ code: 200, data });

    await get_history_by_turn_page('s1', 5, 20, 2);
    expect(mock).toHaveBeenCalledWith({
      url: '/get_history_by_turn_page',
      opts: {
        session_id: 's1',
        min_turn_num: 5,
        turn_page_size: 20,
        turn_page_num: 2,
      },
      method: 'get',
    });
  });

  it('returns [] when data is falsy and cache empty', async () => {
    stubFetchApi({ code: 200, data: null });
    await expect(get_history_by_turn_page('s1', 0, 10, 1)).resolves.toEqual([]);
  });

  it('clamps caller-passed min_turn_num=0 to 1 when cache is empty (server requires >= 1)', async () => {
    // Cache empty => cachedMaxTurnNum = 0, caller passes 0 => must be clamped to 1,
    // otherwise the server-side Pydantic validation rejects it (>= 1).
    const data = [{ ...rows[0], id: 10, turn_num: 1 }];
    const mock = stubFetchApi({ code: 200, data });

    await get_history_by_turn_page('s1', 0, 10, 1);

    expect(mock).toHaveBeenCalledWith({
      url: '/get_history_by_turn_page',
      opts: {
        session_id: 's1',
        min_turn_num: 1,
        turn_page_size: 10,
        turn_page_num: 1,
      },
      method: 'get',
    });
  });

  it('falls back to cache when fetchApi rejects', async () => {
    mockDb.readCachedMessages.mockResolvedValue([rows[0]]);
    stubFetchApi(Promise.reject(new Error('boom')));
    await expect(get_history_by_turn_page('s1', 0, 10, 1)).resolves.toEqual([rows[0]]);
    expect(mockDb.cacheMessages).not.toHaveBeenCalled();
  });
});

describe('clearSession', () => {
  it('returns true and purges cache on success', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(clearSession('abc')).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/sessions',
      opts: { session_id: 'abc' },
      method: 'delete',
    });
    expect(mockDb.clearCachedSession).toHaveBeenCalledWith('abc');
  });

  it('returns false on failure and does not purge cache', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(clearSession('abc')).resolves.toBe(false);
    expect(mockDb.clearCachedSession).not.toHaveBeenCalled();
  });
});

describe('postAgentStream', () => {
  let sockets: FakeWebSocket[];

  beforeEach(() => {
    sockets = [];
    vi.stubGlobal(
      'WebSocket',
      class FakeWebSocket {
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
          sockets.push(this);
        }
        send(data: string) {
          this.sent.push(data);
        }
        close() {
          this.closed = true;
          this.readyState = FakeWebSocket.CLOSED;
        }
        // Simulate the backend opening the connection; mirrors the real
        // WebSocket readyState transition to OPEN.
        open() {
          this.readyState = FakeWebSocket.OPEN;
          this.onopen?.({});
        }
      } as unknown as typeof WebSocket,
    );
  });

  it('opens the WS bridge, sends the request, and calls onData with each chunk', async () => {
    const onData = vi.fn();
    const onDone = vi.fn();

    const promise = new Promise<void>((resolve, reject) => {
      postAgentStream('s1', { text: 'hi' }, onData, () => {
        onDone();
        resolve();
      }, reject);
    });

    const ws = sockets[0];
    expect(ws.url).toBe('ws://localhost:8080/sessions/agent/ws');
    ws.open();
    // Tauri mode guarded out; browser uses streamChatMessage -> sendChatMessageWs.
    expect(ws.sent).toEqual([
      JSON.stringify({
        session_id: 's1',
        multi_modal_message: { text: 'hi', image_base64_list: [], image_path_list: [] },
      }),
    ]);

    ws.onmessage?.({ data: JSON.stringify({ event: 'chunk', session_id: 's1', content: 'hel', type: 'text' }) });
    ws.onmessage?.({ data: JSON.stringify({ event: 'chunk', session_id: 's1', content: 'lo', type: 'text' }) });
    expect(onData).toHaveBeenCalledTimes(2);
    expect(onData).toHaveBeenNthCalledWith(1, 'hel', 'text', 's1');
    expect(onData).toHaveBeenNthCalledWith(2, 'lo', 'text', 's1');

    ws.onmessage?.({ data: JSON.stringify({ event: 'done', session_id: 's1', content: '' }) });
    await promise;
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('sends the request body with the given session id/text', async () => {
    const done = new Promise<void>((resolve, reject) => {
      postAgentStream('s7', { text: 'ping' }, () => {}, () => resolve(), reject);
    });
    const ws = sockets[0];
    ws.open();
    ws.onmessage?.({ data: JSON.stringify({ event: 'done', session_id: 's7', content: '' }) });
    await done;

    expect(ws.sent).toEqual([
      JSON.stringify({
        session_id: 's7',
        multi_modal_message: { text: 'ping', image_base64_list: [], image_path_list: [] },
      }),
    ]);
  });

  it('calls onError on an error frame', async () => {
    const onError = vi.fn();
    const promise = new Promise<void>((resolve) => {
      postAgentStream('s1', { text: 'x' }, () => {}, () => {}, onError);
      setTimeout(resolve, 20);
    });
    const ws = sockets[0];
    ws.open();
    ws.onmessage?.({ data: JSON.stringify({ event: 'error', session_id: 's1', content: 'boom' }) });
    await promise;
    expect(onError).toHaveBeenCalled();
  });

  it('calls onError on socket error', async () => {
    const onError = vi.fn();
    const promise = new Promise<void>((resolve) => {
      postAgentStream('s1', { text: 'x' }, () => {}, () => {}, onError);
      setTimeout(resolve, 20);
    });
    const ws = sockets[0];
    ws.open();
    ws.onerror?.({});
    await promise;
    expect(onError).toHaveBeenCalled();
  });

  it('does not call onError when the request is aborted', async () => {
    const onError = vi.fn();
    const controller = postAgentStream('s1', { text: 'x' }, () => {}, () => {}, onError);
    const ws = sockets[0];
    ws.open();

    controller.abort();

    await new Promise((r) => setTimeout(r, 30));
    expect(onError).not.toHaveBeenCalled();
    // Stop frame is sent over the same socket before teardown.
    expect(ws.sent.some((s) => JSON.parse(s).type === 'stop')).toBe(true);
  });
});
