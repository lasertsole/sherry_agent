/**
 * Unit tests for the character-display snapshot cache wrappers in `../db`
 * (cacheCharacter / readCachedCharacter / clearCachedCharacter +
 * DEFAULT_CACHED_CHARACTER), run against the REAL `HistoryDb` singleton over
 * fake-indexeddb — only the IndexedDB backend is faked, the wrappers under
 * test are the real ones (the pre-rewrite version re-implemented them inside
 * a vi.mock factory, so it could only ever test itself).
 *
 * `import 'fake-indexeddb/auto'` MUST stay the first import: it injects the
 * fake IndexedDB implementation into globalThis before any module below
 * evaluates (happy-dom has no IndexedDB, and `../db` creates its Dexie
 * singleton at module scope).
 *
 * Isolation: every test starts from a wiped database (`db.delete()` +
 * `db.open()`; Dexie 4 refuses to auto-reopen a deleted instance, so reopen
 * explicitly — this recreates every table empty).
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach } from 'vitest';
import {
  db,
  GLOBAL_SESSION_KEY,
  DEFAULT_CACHED_CHARACTER,
  cacheCharacter,
  readCachedCharacter,
  clearCachedCharacter
} from '../db';
import type { CachedCharacter } from '../db';

const snapshot: CachedCharacter = {
  session_id: 'ses_A',
  userName: '用户',
  userAvatar: 'data:image/png;base64,AAAB',
  aiName: 'Sherry',
  aiAvatar: 'data:image/png;base64,BBBC'
};

beforeEach(async () => {
  await db.delete();
  await db.open();
});

describe('cacheCharacter / readCachedCharacter', () => {
  it('round-trips a session snapshot keyed by session_id', async () => {
    await cacheCharacter(snapshot);
    await expect(readCachedCharacter('ses_A')).resolves.toEqual(snapshot);
  });

  it('persists the global-pending profile under GLOBAL_SESSION_KEY', async () => {
    const globalProfile: CachedCharacter = {
      session_id: GLOBAL_SESSION_KEY,
      userName: '待定',
      userAvatar: '',
      aiName: 'AI',
      aiAvatar: ''
    };
    await cacheCharacter(globalProfile);
    await expect(readCachedCharacter(GLOBAL_SESSION_KEY)).resolves.toEqual(globalProfile);
  });

  it('resolves to undefined when no snapshot exists for the session', async () => {
    await expect(readCachedCharacter('ses_MISSING')).resolves.toBeUndefined();
  });

  it('keeps distinct snapshots isolated per session (old sessions unaffected)', async () => {
    // Two sessions that captured different global snapshots at open time.
    const oldSession: CachedCharacter = { ...snapshot, session_id: 'ses_OLD', aiName: '旧名' };
    const newSession: CachedCharacter = { ...snapshot, session_id: 'ses_NEW', aiName: '新名' };
    await cacheCharacter(oldSession);
    await cacheCharacter(newSession);

    await expect(readCachedCharacter('ses_OLD')).resolves.toEqual(oldSession);
    await expect(readCachedCharacter('ses_NEW')).resolves.toEqual(newSession);
    // Updating the new session's snapshot must not change what the old one reads back.
    await cacheCharacter({ ...newSession, aiName: '改名' });
    await expect(readCachedCharacter('ses_OLD')).resolves.toEqual(oldSession);
    await expect(readCachedCharacter('ses_NEW')).resolves.toMatchObject({ aiName: '改名' });
  });
});

describe('DEFAULT_CACHED_CHARACTER (builtin defaults)', () => {
  it('carries the four display fields as non-empty strings', () => {
    // Structural contract only: the concrete default names/avatars live in
    // defaultCharacter.ts and must not be duplicated here.
    for (const field of ['userName', 'userAvatar', 'aiName', 'aiAvatar'] as const) {
      expect(typeof DEFAULT_CACHED_CHARACTER[field]).toBe('string');
      expect(DEFAULT_CACHED_CHARACTER[field].length).toBeGreaterThan(0);
    }
  });

  it('is usable as the fallback shape for a CachedCharacter snapshot', async () => {
    const fallback = { session_id: GLOBAL_SESSION_KEY, ...DEFAULT_CACHED_CHARACTER };
    await cacheCharacter(fallback);
    await expect(readCachedCharacter(GLOBAL_SESSION_KEY)).resolves.toEqual(fallback);
  });
});

describe('clearCachedCharacter', () => {
  it('removes the row so subsequent reads return undefined, global row survives', async () => {
    await cacheCharacter(snapshot);
    await cacheCharacter({ ...snapshot, session_id: GLOBAL_SESSION_KEY });

    await clearCachedCharacter('ses_A');

    await expect(readCachedCharacter('ses_A')).resolves.toBeUndefined();
    await expect(readCachedCharacter(GLOBAL_SESSION_KEY)).resolves.toMatchObject({
      session_id: GLOBAL_SESSION_KEY
    });
  });
});
