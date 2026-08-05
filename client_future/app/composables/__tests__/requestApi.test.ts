import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchApi } from '../requestApi';

// `useFetch` is a Nuxt auto-import, globally stubbed in setup.ts.
// Tests restub it per case and assert on how fetchApi delegates to it,
// since the internal helpers (replacePathVariables / retryFetch) are
// module-private and only observable through fetchApi.

function stubUseFetch(data: unknown) {
  (globalThis as any).useFetch = vi.fn().mockReturnValue({
    data: { value: data },
    error: { value: null },
  });
  return (globalThis as any).useFetch;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('fetchApi -> useFetch delegation', () => {
  it('returns the response data on success', async () => {
    stubUseFetch({ code: 200, data: [1, 2, 3] });
    const result = await fetchApi({ url: '/items', method: 'get' });
    expect(result).toEqual({ code: 200, data: [1, 2, 3] });
  });

  it('calls useFetch with the request URL unchanged when no path params', async () => {
    stubUseFetch({ code: 200 });
    await fetchApi({ url: '/items', method: 'get' });
    const [[requestURL]] = (globalThis as any).useFetch.mock.calls;
    expect(requestURL).toBe('/items');
  });

  it('substitutes :param placeholders in the URL from opts', async () => {
    stubUseFetch({ code: 200 });
    await fetchApi({
      url: '/sessions/:session_id/messages/:id',
      opts: { session_id: 'abc', id: '42' },
      method: 'get',
    });
    const [[requestURL, options]] = (globalThis as any).useFetch.mock.calls;
    expect(requestURL).toBe('/sessions/abc/messages/42');
    // used keys should be removed from the query params
    expect(options.query).toEqual({});
  });

  it('throws when a path param is missing from params', async () => {
    stubUseFetch({ code: 200 });
    // non-empty opts so replacement is attempted, but the placeholder key is absent
    await expect(
      fetchApi({ url: '/users/:id', opts: { other: 'x' }, method: 'get' }),
    ).rejects.toThrow('"id" is not provided in params');
  });

  it('passes opts as query for GET and as body for POST', async () => {
    stubUseFetch({ code: 200 });

    await fetchApi({ url: '/search', opts: { q: 'vue' }, method: 'get' });
    await fetchApi({ url: '/create', opts: { name: 'item' }, method: 'post' });

    const useFetchMock = (globalThis as any).useFetch;
    expect(useFetchMock.mock.calls[0][1].query).toEqual({ q: 'vue' });
    expect(useFetchMock.mock.calls[1][1].body).toEqual({ name: 'item' });
    expect(useFetchMock.mock.calls[1][1].method).toBe('post');
  });

  it('sets baseURL from VITE_API_BACK_URL', async () => {
    stubUseFetch({ code: 200 });
    await fetchApi({ url: '/x', method: 'get' });
    expect((globalThis as any).useFetch.mock.calls[0][1].baseURL).toBe(
      'http://localhost:8080',
    );
  });

  it('sets the Content-Type header for non-GET requests via the onRequest interceptor', async () => {
    let capturedHeaders: Headers | null = null;
    (globalThis as any).useFetch = vi.fn().mockImplementation((_u: string, opts: any) => {
      const headers = new Headers();
      opts.onRequest?.({ request: _u, options: { headers } });
      capturedHeaders = headers;
      return { data: { value: { code: 200 } }, error: { value: null } };
    });

    await fetchApi({
      url: '/save',
      opts: { a: 1 },
      method: 'post',
      contentType: 'application/json',
    });
    expect(capturedHeaders!.get('Content-Type')).toBe('application/json');
  });

  it('does NOT set a Content-Type header for GET requests', async () => {
    let capturedHeaders: Headers | null = null;
    (globalThis as any).useFetch = vi.fn().mockImplementation((_u: string, opts: any) => {
      const headers = new Headers();
      opts.onRequest?.({ request: _u, options: { headers } });
      capturedHeaders = headers;
      return { data: { value: {} }, error: { value: null } };
    });

    await fetchApi({ url: '/list', method: 'get' });
    expect(capturedHeaders!.has('Content-Type')).toBe(false);
  });
});

describe('fetchApi error handling', () => {
  it('retries the request when useFetch rejects once, then succeeds', async () => {
    const useFetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error('temporary'))
      .mockResolvedValueOnce({ data: { value: { code: 200 } }, error: { value: null } });
    (globalThis as any).useFetch = useFetchMock;

    const result = await fetchApi({ url: '/retry', method: 'get' });
    expect(result).toEqual({ code: 200 });
    expect(useFetchMock).toHaveBeenCalledTimes(2);
  });

  it('propagates the error after exhausting retries', async () => {
    const useFetchMock = vi.fn().mockRejectedValue(new Error('network down'));
    (globalThis as any).useFetch = useFetchMock;

    await expect(fetchApi({ url: '/boom', method: 'get' })).rejects.toThrow('network down');
    // initial call + at least one retry
    expect(useFetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  }, 15000);
});
