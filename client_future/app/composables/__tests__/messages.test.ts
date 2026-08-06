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
    { id: 1, turn_num: 1, session_id: 's1', role: 'human', content: 'hi', timestamp: null, tool_call_id: null, tool_calls: null, tool_status: null, tool_name: null, finish_reason: null, reasoning: null, reasoning_content: null },
    { id: 2, turn_num: 2, session_id: 's1', role: 'ai', content: 'hello', timestamp: null, tool_call_id: null, tool_calls: null, tool_status: null, tool_name: null, finish_reason: null, reasoning: null, reasoning_content: null },
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
  function sseResponse(events: string): Response {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(events));
        controller.close();
      },
    });
    return {
      ok: true,
      status: 200,
      body,
    } as unknown as Response;
  }

  it('parses SSE data lines and calls onData with each chunk', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue(
      sseResponse('data: hello\ndata: world\n\n'),
    );

    const onData = vi.fn();
    const onDone = vi.fn();

    await new Promise<void>((resolve, reject) => {
      postAgentStream('s1', { text: 'hi' }, onData, onDone, reject);
      setTimeout(() => resolve(), 20);
    });

    expect(onData).toHaveBeenCalledTimes(2);
    expect(onData).toHaveBeenNthCalledWith(1, 'hello');
    expect(onData).toHaveBeenNthCalledWith(2, 'world');
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('POSTs to the SSE endpoint with JSON body and content-type header', async () => {
    const fetchMock = vi.fn().mockResolvedValue(sseResponse('data: chunk\n\n'));
    (globalThis as any).fetch = fetchMock;

    await new Promise<void>((resolve) => {
      postAgentStream('s7', { text: 'ping' }, () => {}, () => resolve());
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('http://localhost:8080/sessions/agent/sse');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body)).toEqual({
      session_id: 's7',
      multi_modal_message: { text: 'ping' },
    });
  });

  it('calls onError when the response is not ok', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 });

    const onError = vi.fn();
    await new Promise<void>((resolve) => {
      postAgentStream('s1', { text: 'x' }, () => {}, () => {}, onError);
      setTimeout(() => resolve(), 20);
    });
    expect(onError).toHaveBeenCalled();
  });

  it('calls onError when the response body is not readable', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({ ok: true, body: null });

    const onError = vi.fn();
    await new Promise<void>((resolve) => {
      postAgentStream('s1', { text: 'x' }, () => {}, () => {}, onError);
      setTimeout(() => resolve(), 20);
    });
    expect(onError).toHaveBeenCalled();
  });

  it('does not call onError when the request is aborted', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue(sseResponse('data: x\n\n'));

    const onError = vi.fn();
    const controller = postAgentStream('s1', { text: 'x' }, () => {}, () => {}, onError);
    controller.abort();

    await new Promise((r) => setTimeout(r, 30));
    expect(onError).not.toHaveBeenCalled();
  });
});
