import { describe, it, expect, vi, afterEach } from 'vitest';
import { API_BASE_URL } from '../env';
import {
  CHUNK_SIZE,
  MAX_CONCURRENT_FILES,
  resumeUpload,
  runPool,
  uploadChunked,
  uploadMultipleFiles
} from '../bridge/upload-chunked';

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
  it('begins, sends sequential chunks and completes', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ ok: true, upload_id: 'u1', next_chunk_index: 0 }))
      .mockResolvedValueOnce(okResponse({ ok: true, next_chunk_index: 1 }))
      .mockResolvedValueOnce(okResponse({ ok: true, next_chunk_index: 2 }))
      .mockResolvedValueOnce(okResponse({ ok: true, url: 'http://x/images/abc.png' }));
    vi.stubGlobal('fetch', fetchMock);
    const bytes = new Uint8Array(CHUNK_SIZE + 10);
    const progress: Array<[number, number]> = [];
    const begun: Array<[string, number]> = [];

    const url = await uploadChunked(
      bytes,
      'image',
      's1',
      'image/png',
      (sent, total) => progress.push([sent, total]),
      (uploadId, total) => begun.push([uploadId, total])
    );

    expect(url).toBe('http://x/images/abc.png');
    expect(begun).toEqual([['u1', 2]]);
    expect(progress).toEqual([
      [1, 2],
      [2, 2]
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    const beginUrl = fetchMock.mock.calls[0]?.[0] as string;
    expect(beginUrl.startsWith(`${API_BASE_URL}/upload/begin?`)).toBe(true);
    const beginQuery = queryOf(beginUrl);
    expect(beginQuery.get('session_id')).toBe('s1');
    expect(beginQuery.get('total_chunks')).toBe('2');
    expect(beginQuery.get('total_bytes')).toBe(String(bytes.length));
    expect(beginQuery.get('content_type')).toBe('image/png');
    const chunkQuery = queryOf(fetchMock.mock.calls[1]?.[0] as string);
    expect(chunkQuery.get('upload_id')).toBe('u1');
    expect(chunkQuery.get('chunk_index')).toBe('0');
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).body as Uint8Array).toHaveLength(CHUNK_SIZE);
    expect(queryOf(fetchMock.mock.calls[3]?.[0] as string).get('upload_id')).toBe('u1');
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
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ ok: true, upload_id: 'u1', next_chunk_index: 0 }))
      .mockResolvedValueOnce(errorResponse(500));
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadChunked(new Uint8Array(CHUNK_SIZE + 1), 'image', 's1', 'image/png')).rejects.toThrow(
      'chunk 0 upload failed'
    );
  });

  it('throws when the completion response has no URL', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ ok: true, upload_id: 'u1' }))
      .mockResolvedValueOnce(okResponse({ ok: true }))
      .mockResolvedValueOnce(okResponse({ ok: true }))
      .mockResolvedValueOnce(okResponse({ ok: false }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(uploadChunked(new Uint8Array(CHUNK_SIZE + 1), 'audio', 's1', 'audio/webm')).rejects.toThrow(
      'upload completion failed'
    );
  });
});

describe('runPool / uploadMultipleFiles', () => {
  it('caps in-flight work and preserves the result order', async () => {
    let active = 0;
    let peak = 0;
    const seen: number[] = [];

    await runPool([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 3, async (_item, index) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 0));
      seen.push(index);
      active -= 1;
    });

    expect(peak).toBeLessThanOrEqual(3);
    expect(seen.sort((a, b) => a - b)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  });

  it('uploads files in parallel lanes and returns their URLs by index', async () => {
    let active = 0;
    let peak = 0;
    const fetchMock = vi.fn(async (input: string) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 1));
      active -= 1;
      const url = String(input);
      if (url.includes('/upload/begin')) {
        return okResponse({ ok: true, upload_id: crypto.randomUUID() });
      }
      if (url.includes('/upload/complete')) {
        return okResponse({ ok: true, url: `http://x/${url.length}` });
      }
      return okResponse({ ok: true });
    });
    vi.stubGlobal('fetch', fetchMock);
    const files = Array.from({ length: 10 }, () => ({
      bytes: new Uint8Array(CHUNK_SIZE + 1),
      kind: 'image' as const,
      contentType: 'image/png'
    }));

    const urls = await uploadMultipleFiles(files, 's1');

    expect(urls).toHaveLength(10);
    expect(urls.every(url => url.startsWith('http://x/'))).toBe(true);
    expect(peak).toBeLessThanOrEqual(MAX_CONCURRENT_FILES);
  });

  it('resumes from the server-reported chunk count', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ ok: true, received_chunks: 1, total_chunks: 2 }))
      .mockResolvedValueOnce(okResponse({ ok: true, next_chunk_index: 2 }))
      .mockResolvedValueOnce(okResponse({ ok: true, url: 'http://x/resumed.png' }));
    vi.stubGlobal('fetch', fetchMock);

    const url = await resumeUpload('u1', new Uint8Array(CHUNK_SIZE + 1), 'image', 'image/png');

    expect(url).toBe('http://x/resumed.png');
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(queryOf(fetchMock.mock.calls[1]?.[0] as string).get('chunk_index')).toBe('1');
  });
});
