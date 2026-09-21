import { describe, it, expect, expectTypeOf, vi, afterEach } from 'vitest';
import { fetchApi, fetchApiPayload, shouldRequestFailureToast } from '../requestApi';

// `$fetch` is ofetch's global, globally stubbed in setup.ts.
// Tests restub it per case and assert on how fetchApi delegates to it,
// since the internal helpers (replacePathVariables / retryFetch) are
// module-private and only observable through fetchApi.

function stubFetch(data: unknown) {
  (globalThis as any).$fetch = vi.fn().mockResolvedValue(data);
  return (globalThis as any).$fetch;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('fetchApi -> $fetch delegation', () => {
  it('returns the response data on success', async () => {
    stubFetch({ code: 200, data: [1, 2, 3] });
    const result = await fetchApi({ url: '/items', method: 'get' });
    expect(result).toEqual({ code: 200, data: [1, 2, 3] });
  });

  it('calls $fetch with the request URL unchanged when no path params', async () => {
    stubFetch({ code: 200 });
    await fetchApi({ url: '/items', method: 'get' });
    const [[requestURL]] = (globalThis as any).$fetch.mock.calls;
    expect(requestURL).toBe('/items');
  });

  it('substitutes :param placeholders in the URL from opts', async () => {
    stubFetch({ code: 200 });
    await fetchApi({
      url: '/sessions/:session_id/messages/:id',
      opts: { session_id: 'abc', id: '42' },
      method: 'get'
    });
    const [[requestURL, options]] = (globalThis as any).$fetch.mock.calls;
    expect(requestURL).toBe('/sessions/abc/messages/42');
    // used keys should be removed from the query params
    expect(options.query).toEqual({});
  });

  it('throws when a path param is missing from params', async () => {
    stubFetch({ code: 200 });
    // non-empty opts so replacement is attempted, but the placeholder key is absent
    await expect(fetchApi({ url: '/users/:id', opts: { other: 'x' }, method: 'get' })).rejects.toThrow(
      '"id" is not provided in params'
    );
  });

  it('passes opts as query for GET and as body for POST', async () => {
    stubFetch({ code: 200 });

    await fetchApi({ url: '/search', opts: { q: 'vue' }, method: 'get' });
    await fetchApi({ url: '/create', opts: { name: 'item' }, method: 'post' });

    const fetchMock = (globalThis as any).$fetch;
    expect(fetchMock.mock.calls[0][1].query).toEqual({ q: 'vue' });
    expect(fetchMock.mock.calls[1][1].body).toEqual({ name: 'item' });
    expect(fetchMock.mock.calls[1][1].method).toBe('post');
  });

  it('sets baseURL from VITE_API_BACK_URL', async () => {
    stubFetch({ code: 200 });
    await fetchApi({ url: '/x', method: 'get' });
    expect((globalThis as any).$fetch.mock.calls[0][1].baseURL).toBe('http://localhost:8080');
  });

  it('sets the Content-Type header for non-GET requests via the onRequest interceptor', async () => {
    let capturedHeaders: Headers | null = null;
    (globalThis as any).$fetch = vi.fn().mockImplementation((_u: string, opts: any) => {
      const headers = new Headers();
      opts.onRequest?.({ request: _u, options: { headers } });
      capturedHeaders = headers;
      return Promise.resolve({ code: 200 });
    });

    await fetchApi({
      url: '/save',
      opts: { a: 1 },
      method: 'post',
      contentType: 'application/json'
    });
    expect(capturedHeaders!.get('Content-Type')).toBe('application/json');
  });

  it('does NOT set a Content-Type header for GET requests', async () => {
    let capturedHeaders: Headers | null = null;
    (globalThis as any).$fetch = vi.fn().mockImplementation((_u: string, opts: any) => {
      const headers = new Headers();
      opts.onRequest?.({ request: _u, options: { headers } });
      capturedHeaders = headers;
      return Promise.resolve({});
    });

    await fetchApi({ url: '/list', method: 'get' });
    expect(capturedHeaders!.has('Content-Type')).toBe(false);
  });
});

describe('fetchApi error handling', () => {
  it('resolves null when $fetch ultimately fails (no throw to caller)', async () => {
    // ofetch-level retries (retry: 3) are delegated to the real $fetch in
    // production; here the stub rejects immediately and fetchApi must keep
    // the historical useFetch contract: resolve with null instead of throwing.
    (globalThis as any).$fetch = vi.fn().mockRejectedValue(new Error('network down'));

    const result = await fetchApi({ url: '/boom', method: 'get' });
    expect(result).toBeNull();
    // swallowed inside the request layer → no retryFetch-level re-invocation
    expect((globalThis as any).$fetch).toHaveBeenCalledTimes(1);
  }, 15000);
});

describe('fetchApi payload typing', () => {
  it('exposes the caller-declared payload as T | null', async () => {
    stubFetch({ jobs: [{ id: 'a' }] });

    const result = await fetchApi<{ jobs: Array<{ id: string }> }>({ url: '/cron', method: 'get' });

    expectTypeOf(result).toEqualTypeOf<{ jobs: Array<{ id: string }> } | null>();
    expect(result).toEqual({ jobs: [{ id: 'a' }] });
  });

  it('fetchApiPayload declares a non-null payload and forwards the failure null unchanged', async () => {
    stubFetch({ jobs: [{ id: 'a' }] });

    const ok = await fetchApiPayload<{ jobs: Array<{ id: string }> }>({ url: '/cron', method: 'get' });

    expectTypeOf(ok).toEqualTypeOf<{ jobs: Array<{ id: string }> }>();
    expect(ok).toEqual({ jobs: [{ id: 'a' }] });

    (globalThis as any).$fetch = vi.fn().mockRejectedValue(new Error('network down'));
    const failed = await fetchApiPayload<{ jobs: Array<{ id: string }> }>({ url: '/cron', method: 'get' });
    // Legacy bridge contract: the failure null is passed through, not replaced by a fallback.
    expect(failed).toBeNull();
  }, 15000);
});

describe('fetchApi response payload validation (audit #51)', () => {
  it('returns object and array payloads unchanged', async () => {
    stubFetch({ code: 200, data: [1, 2, 3] });
    await expect(fetchApi({ url: '/items', method: 'get' })).resolves.toEqual({
      code: 200,
      data: [1, 2, 3]
    });

    stubFetch([{ id: 1 }]);
    await expect(fetchApi({ url: '/sessions', method: 'get' })).resolves.toEqual([{ id: 1 }]);
  });

  it('returns legacy text payloads unchanged (text/plain "None")', async () => {
    stubFetch('None');
    await expect(fetchApi({ url: '/get_pending_interrupt', method: 'get' })).resolves.toBe('None');
  });

  it.each([
    ['a number', 42],
    ['a boolean', true],
    ['an undefined body', undefined]
  ])('rejects %s payload by resolving null', async (_label, payload) => {
    stubFetch(payload);
    await expect(fetchApi({ url: '/weird', method: 'get' })).resolves.toBeNull();
  });
});

describe('error-handling strategy: boundary toast decision', () => {
  it('toasts on a network failure even when a payload is absent or present', () => {
    const base = { requestFailed: false, httpFailed: false };
    expect(shouldRequestFailureToast({ networkFailed: true, ...base, hasData: false })).toBe(true);
    expect(shouldRequestFailureToast({ networkFailed: true, ...base, hasData: true })).toBe(true);
  });

  it('toasts on an unexpected/thrown failure', () => {
    expect(
      shouldRequestFailureToast({ networkFailed: false, requestFailed: true, httpFailed: false, hasData: false })
    ).toBe(true);
  });

  it('toasts on an HTTP failure only when no usable payload was resolved', () => {
    expect(
      shouldRequestFailureToast({ networkFailed: false, requestFailed: false, httpFailed: true, hasData: false })
    ).toBe(true);
    expect(
      shouldRequestFailureToast({ networkFailed: false, requestFailed: false, httpFailed: true, hasData: true })
    ).toBe(false);
  });

  it('stays silent on success (a later retry resolved a payload)', () => {
    expect(
      shouldRequestFailureToast({ networkFailed: false, requestFailed: false, httpFailed: false, hasData: true })
    ).toBe(false);
  });
});
