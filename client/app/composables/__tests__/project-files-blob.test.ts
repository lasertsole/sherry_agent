import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The project-files bridge's binary half: `fetchProjectFileBlob` rides the raw
 * transport (`fetchApiRaw`) because the answer is bytes, and its failure
 * contract surfaces the backend's refusal `reason` verbatim — the viewer shows
 * it in the panel's error line.
 */
const mocks = vi.hoisted(() => ({
  fetchApiRaw: vi.fn()
}));

vi.mock('../requestApi', () => ({
  fetchApiPayload: vi.fn(),
  fetchApiRaw: mocks.fetchApiRaw
}));

import { fetchProjectFileBlob } from '../bridge/project-files';

/**
 * A raw-transport response double.
 * @param status
 * @param body
 * @param contentType
 */
function blobResponse(status: number, body: ArrayBuffer | object, contentType?: string) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(contentType ? { 'Content-Type': contentType } : {}),
    arrayBuffer: async () => body as ArrayBuffer,
    json: async () => body
  };
}

describe('fetchProjectFileBlob', () => {
  beforeEach(() => {
    mocks.fetchApiRaw.mockReset();
  });

  it('returns the bytes and the mime from Content-Type', async () => {
    const payload = new TextEncoder().encode('%PDF-1.7').buffer;
    mocks.fetchApiRaw.mockResolvedValueOnce(blobResponse(200, payload, 'application/pdf'));

    const result = await fetchProjectFileBlob('sess-1', 'doc.pdf');

    expect(result).toEqual({ bytes: payload, mime: 'application/pdf' });
    const url = String(mocks.fetchApiRaw.mock.calls[0]![0].url);
    expect(url.startsWith('/project/file/blob?')).toBe(true);
    expect(url).toContain('session_id=sess-1');
    expect(url).toContain('path=doc.pdf');
  });

  it('falls back to octet-stream without a Content-Type header', async () => {
    const payload = new ArrayBuffer(3);
    mocks.fetchApiRaw.mockResolvedValueOnce(blobResponse(200, payload));

    const result = await fetchProjectFileBlob('sess-1', 'weird.qqqzz');
    expect(result.mime).toBe('application/octet-stream');
  });

  it('strips mime parameters (charset suffixes)', async () => {
    mocks.fetchApiRaw.mockResolvedValueOnce(blobResponse(200, new ArrayBuffer(1), 'text/markdown; charset=utf-8'));
    const result = await fetchProjectFileBlob('sess-1', 'README.md');
    expect(result.mime).toBe('text/markdown');
  });

  it('surfaces the backend refusal reason verbatim', async () => {
    mocks.fetchApiRaw.mockResolvedValueOnce(
      blobResponse(413, { success: false, reason: 'file too large (99 bytes, limit 48)' })
    );

    await expect(fetchProjectFileBlob('sess-1', 'big.pdf')).rejects.toThrow('file too large (99 bytes, limit 48)');
  });

  it('falls back to the HTTP status on a non-JSON refusal body', async () => {
    mocks.fetchApiRaw.mockResolvedValueOnce(blobResponse(500, 'internal error' as unknown as object));
    await expect(fetchProjectFileBlob('sess-1', 'x.pdf')).rejects.toThrow('HTTP 500');
  });

  it('propagates a network failure unchanged', async () => {
    mocks.fetchApiRaw.mockRejectedValueOnce(new TypeError('network down'));
    await expect(fetchProjectFileBlob('sess-1', 'x.pdf')).rejects.toThrow('network down');
  });
});
