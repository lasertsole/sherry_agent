import { describe, it, expect, vi, afterEach } from 'vitest';
import { uploadBase64ToUrls, KIND_LABEL } from '../bridge/upload';
import { API_BASE_URL } from '../env';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const okResponse = (json: unknown) => ({ ok: true, status: 200, json: async () => json }) as unknown as Response;

describe('uploadBase64ToUrls', () => {
  it('uploads one image through the shared client with the per-kind default content type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true, url: 'http://x/i.png' }));
    vi.stubGlobal('fetch', fetchMock);

    const urls = await uploadBase64ToUrls('image', ['AAAA']);

    expect(urls).toEqual(['http://x/i.png']);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_BASE_URL}/images/upload`);
    expect(init.method).toBe('post');
    expect((init.headers as Headers).get('Content-Type')).toBe('image/png');
    expect(init.body).toBeInstanceOf(Uint8Array);
  });

  it('keeps the existing data: prefix handling (strips the prefix and uses its MIME when it matches)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true, url: 'http://x/i' }));
    vi.stubGlobal('fetch', fetchMock);

    // The prefix regex only accepts [w.+-] MIME characters (long-standing behavior, unchanged).
    await uploadBase64ToUrls('image', ['data:image/w+;base64,AAAA']);

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Headers).get('Content-Type')).toBe('image/w+');
  });

  it('uses the per-kind default content types when no data: prefix is present', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true, url: 'http://x/a' }));
    vi.stubGlobal('fetch', fetchMock);

    await uploadBase64ToUrls('audio', ['QUJD']);
    await uploadBase64ToUrls('video', ['RUZH']);

    const audioInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const videoInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect((audioInit.headers as Headers).get('Content-Type')).toBe('audio/webm');
    expect((videoInit.headers as Headers).get('Content-Type')).toBe('video/mp4');
    expect(fetchMock.mock.calls[1]?.[0]).toBe(`${API_BASE_URL}/video/upload`);
  });

  it('uploads multiple entries sequentially preserving the input order', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ success: true, url: 'u1' }))
      .mockResolvedValueOnce(okResponse({ success: true, url: 'u2' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadBase64ToUrls('image', ['AAAA', 'BBBB'])).resolves.toEqual(['u1', 'u2']);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('throws a labelled HTTP error without retrying on a non-ok response', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 503 } as Response);
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadBase64ToUrls('image', ['AAAA'])).rejects.toThrow('Image upload failed: HTTP 503');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('throws a labelled error for a non-JSON response body', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => {
        throw new Error('bad json');
      }
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadBase64ToUrls('video', ['AAAA'])).rejects.toThrow(
      'Video upload failed: server returned non-JSON response'
    );
  });

  it('throws a labelled error when the payload reports !success or is missing the URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: false, error: 'nope' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadBase64ToUrls('audio', ['AAAA'])).rejects.toThrow(
      `Audio upload failed: ${JSON.stringify({ success: false, error: 'nope' })}`
    );
  });

  it('wraps a transport failure as a labelled network error with the original cause', async () => {
    const cause = new Error('net down');
    const fetchMock = vi.fn().mockRejectedValue(cause);
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadBase64ToUrls('image', ['AAAA'])).rejects.toThrow(
      `${KIND_LABEL.image} upload network error: Error: net down`
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
