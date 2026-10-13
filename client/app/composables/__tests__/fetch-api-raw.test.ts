import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchApiRaw } from '../requestApi';
import { API_BASE_URL } from '../env';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('fetchApiRaw', () => {
  it('calls the shared base URL and returns the raw Response untouched', async () => {
    const response = { ok: true, status: 200, json: async () => ({ success: true }) } as unknown as Response;
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetchMock);

    const result = await fetchApiRaw({ url: '/system_prompt' });

    expect(result).toBe(response);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API_BASE_URL}/system_prompt`);
  });

  it('forwards method, raw body and explicit Content-Type', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal('fetch', fetchMock);
    const body = new Uint8Array([1, 2, 3]);

    await fetchApiRaw({ url: '/images/upload', method: 'post', body, contentType: 'image/png' });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('post');
    expect(init.body).toBe(body);
    expect((init.headers as Headers).get('Content-Type')).toBe('image/png');
  });

  it('rejects with the underlying fetch error unchanged (no swallow, no retry)', async () => {
    const err = new Error('net down');
    const fetchMock = vi.fn().mockRejectedValue(err);
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchApiRaw({ url: '/system_prompt' })).rejects.toBe(err);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
