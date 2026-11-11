import { describe, it, expect, vi, afterEach } from 'vitest';
import { API_BASE_URL } from '../env';
import { CHUNK_SIZE, uploadChunked } from '../bridge/upload-chunked';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const okResponse = (json: unknown) => ({ ok: true, status: 200, json: async () => json }) as unknown as Response;
const errorResponse = (status: number) => ({ ok: false, status, json: async () => ({}) }) as unknown as Response;

function queryOf(url: string): URLSearchParams {
  return new URLSearchParams(url.split('?')[1] ?? '');
}

describe('uploadChunked', () => {
  it('splits a multi-chunk payload and completes the upload', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ ok: true }))
      .mockResolvedValueOnce(okResponse({ ok: true }))
      .mockResolvedValueOnce(okResponse({ ok: true, url: 'http://x/images/abc.png' }));
    vi.stubGlobal('fetch', fetchMock);
    const bytes = new Uint8Array(CHUNK_SIZE + 10);
    const progress: Array<[number, number]> = [];

    const url = await uploadChunked(bytes, 'image', 's1', 'image/png', (sent, total) => progress.push([sent, total]));

    expect(url).toBe('http://x/images/abc.png');
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [firstUrl, firstInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(firstUrl.startsWith(`${API_BASE_URL}/upload/chunk?`)).toBe(true);
    const firstQuery = queryOf(firstUrl);
    expect(firstQuery.get('session_id')).toBe('s1');
    expect(firstQuery.get('chunk_index')).toBe('0');
    expect(firstQuery.get('total_chunks')).toBe('2');
    expect(firstQuery.get('content_type')).toBe('image/png');
    expect((firstInit.body as Uint8Array).byteLength).toBe(CHUNK_SIZE);
    const secondQuery = queryOf(fetchMock.mock.calls[1]?.[0] as string);
    expect(secondQuery.get('chunk_index')).toBe('1');
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).body as Uint8Array).toHaveLength(10);
    const completeUrl = fetchMock.mock.calls[2]?.[0] as string;
    expect(completeUrl.startsWith(`${API_BASE_URL}/upload/complete?`)).toBe(true);
    expect(queryOf(completeUrl).get('upload_id')).toBe(firstQuery.get('upload_id'));
    expect(progress).toEqual([
      [CHUNK_SIZE, bytes.length],
      [bytes.length, bytes.length]
    ]);
  });

  it('keeps a single-chunk payload on the whole-body endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true, url: 'http://x/i.png' }));
    vi.stubGlobal('fetch', fetchMock);

    const url = await uploadChunked(new Uint8Array(10), 'image', 's1', 'image/png');

    expect(url).toBe('http://x/i.png');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API_BASE_URL}/images/upload`);
  });

  it('throws on a failed chunk', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(errorResponse(500)));

    await expect(uploadChunked(new Uint8Array(CHUNK_SIZE + 1), 'image', 's1', 'image/png')).rejects.toThrow(
      'chunk 0 upload failed: HTTP 500'
    );
  });

  it('throws when the completion response has no URL', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ ok: true }))
      .mockResolvedValueOnce(okResponse({ ok: true }))
      .mockResolvedValueOnce(okResponse({ ok: false }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadChunked(new Uint8Array(CHUNK_SIZE + 1), 'audio', 's1', 'audio/webm')).rejects.toThrow(
      'upload completion failed'
    );
  });
});
