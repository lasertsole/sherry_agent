import { describe, it, expect, vi, afterEach } from 'vitest';
import type { Response } from '@/types/response';

// `fetchApi` is used inside messages.ts as a Nuxt auto-import (no explicit
// `import` statement), so it must be stubbed as a global rather than via
// `vi.mock('../requestApi')`.
import {
  get_history_by_page,
  clearSession,
  postAgentStream,
} from '../messages';

function stubFetchApi(data: unknown) {
  const mock = vi.fn().mockResolvedValue(data);
  vi.stubGlobal('fetchApi', mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('get_history_by_page', () => {
  it('returns res.data when present', async () => {
    const data = [{ type: 'ai' }, { type: 'human' }];
    const mock = stubFetchApi({ code: 200, data });
    const result = await get_history_by_page('s1', 0, 10, 1);
    expect(result).toEqual(data);
    expect(mock).toHaveBeenCalledWith({
      url: '/get_history_by_page',
      opts: {
        session_id: 's1',
        min_turn_num: 0,
        turn_page_size: 10,
        turn_page_num: 1,
      },
      method: 'get',
    });
  });

  it('returns [] when data is falsy', async () => {
    stubFetchApi({ code: 200, data: null });
    await expect(get_history_by_page('s1', 0, 10, 1)).resolves.toEqual([]);
  });

  it('returns [] when fetchApi rejects', async () => {
    stubFetchApi(Promise.reject(new Error('boom')));
    await expect(get_history_by_page('s1', 0, 10, 1)).resolves.toEqual([]);
  });
});

describe('clearSession', () => {
  it('returns true on success', async () => {
    const mock = stubFetchApi({ code: 200 });
    await expect(clearSession('abc')).resolves.toBe(true);
    expect(mock).toHaveBeenCalledWith({
      url: '/sessions',
      opts: { session_id: 'abc' },
      method: 'delete',
    });
  });

  it('returns false on failure', async () => {
    stubFetchApi(Promise.reject(new Error('down')));
    await expect(clearSession('abc')).resolves.toBe(false);
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
