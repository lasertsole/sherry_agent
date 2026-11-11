/**
 * Short-TTL upload dedupe over the REAL Dexie singleton (fake-indexeddb).
 *
 * `import 'fake-indexeddb/auto'` MUST stay first: `../db` creates the Dexie
 * singleton at module scope. The DB is wiped and reopened per test (Dexie 4
 * refuses to auto-reopen after `delete()`).
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { db } from '../db';
import { UPLOAD_CACHE_TTL_MS, uploadWithCache } from '../bridge/upload-cache';

beforeEach(async () => {
  await db.delete();
  await db.open();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const okResponse = (json: unknown) => ({ ok: true, status: 200, json: async () => json }) as unknown as Response;

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('');
}

describe('uploadWithCache', () => {
  it('reuses the URL for byte-identical payloads inside the TTL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true, url: 'http://x/i.png' }));
    vi.stubGlobal('fetch', fetchMock);
    const bytes = new TextEncoder().encode('same image');

    const first = await uploadWithCache(bytes, 'image', 's1', 'image/png');
    const second = await uploadWithCache(bytes.slice(), 'image', 's1', 'image/png');

    expect(first).toBe('http://x/i.png');
    expect(second).toBe(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('uploads different bytes separately', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ success: true, url: 'http://x/a.png' }))
      .mockResolvedValueOnce(okResponse({ success: true, url: 'http://x/b.png' }));
    vi.stubGlobal('fetch', fetchMock);

    const first = await uploadWithCache(new TextEncoder().encode('one'), 'image', 's1', 'image/png');
    const second = await uploadWithCache(new TextEncoder().encode('two'), 'image', 's1', 'image/png');

    expect([first, second]).toEqual(['http://x/a.png', 'http://x/b.png']);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('re-uploads after the cached row expires', async () => {
    const bytes = new TextEncoder().encode('expired');
    const hash = await sha256Hex(bytes);
    await db.uploadCache.put({
      sha256: hash,
      url: 'http://x/stale.png',
      session_id: 's1',
      created_at: Date.now() - UPLOAD_CACHE_TTL_MS - 1
    });
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true, url: 'http://x/fresh.png' }));
    vi.stubGlobal('fetch', fetchMock);

    const url = await uploadWithCache(bytes, 'image', 's1', 'image/png');

    expect(url).toBe('http://x/fresh.png');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const rows = await db.uploadCache.where('sha256').equals(hash).toArray();
    expect(rows.map(row => row.url)).toEqual(['http://x/fresh.png']);
  });

  it('does not cache a failed upload', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) })
      .mockResolvedValueOnce(okResponse({ success: true, url: 'http://x/retry.png' }));
    vi.stubGlobal('fetch', fetchMock);
    const bytes = new TextEncoder().encode('retry');

    await expect(uploadWithCache(bytes, 'image', 's1', 'image/png')).rejects.toThrow('HTTP 500');
    await expect(uploadWithCache(bytes, 'image', 's1', 'image/png')).resolves.toBe('http://x/retry.png');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
