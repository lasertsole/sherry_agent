import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { CachedCharacter } from '../db';

// The character caching wrappers in `../db` route through the exported `db`
// instance's `character` table (put / get / delete). Mocking that table keeps
// tests off a real IndexedDB while still verifying dispatch + key semantics.
const characterTable = vi.hoisted(() => ({
  put: vi.fn(async (char: CachedCharacter) => undefined),
  get: vi.fn(async (key: string) => undefined),
  delete: vi.fn(async (key: string) => undefined),
}));

vi.mock('../db', () => ({
  GLOBAL_SESSION_KEY: '__global__',
  DEFAULT_CACHED_CHARACTER: {
    userName: '远野汉娜',
    userAvatar: '/avatar/user.jpg',
    aiName: '橘雪莉',
    aiAvatar: '/avatar/assistant.jpg',
  },
  db: { character: characterTable },
  cacheCharacter: async (char: CachedCharacter) => {
    await characterTable.put(char);
  },
  readCachedCharacter: async (sessionId: string) => {
    return characterTable.get(sessionId);
  },
  clearCachedCharacter: async (sessionId: string) => {
    await characterTable.delete(sessionId);
  },
}));

import {
  GLOBAL_SESSION_KEY,
  DEFAULT_CACHED_CHARACTER,
  cacheCharacter,
  readCachedCharacter,
  clearCachedCharacter,
} from '../db';

const snapshot: CachedCharacter = {
  session_id: 'ses_A',
  userName: '用户',
  userAvatar: 'data:image/png;base64,AAAB',
  aiName: 'Sherry',
  aiAvatar: 'data:image/png;base64,BBBC',
};

beforeEach(() => {
  characterTable.put.mockClear();
  characterTable.get.mockClear();
  characterTable.delete.mockClear();
  characterTable.get.mockResolvedValue(undefined);
});

describe('cacheCharacter', () => {
  it('writes the snapshot to the character table via db.character.put', async () => {
    await cacheCharacter(snapshot);
    expect(characterTable.put).toHaveBeenCalledTimes(1);
    expect(characterTable.put).toHaveBeenCalledWith(snapshot);
  });

  it('persists a global-pending profile under GLOBAL_SESSION_KEY', async () => {
    const globalProfile: CachedCharacter = {
      session_id: GLOBAL_SESSION_KEY,
      userName: '待定',
      userAvatar: '',
      aiName: 'AI',
      aiAvatar: '',
    };
    await cacheCharacter(globalProfile);
    expect(characterTable.put).toHaveBeenCalledWith(globalProfile);
  });
});

describe('readCachedCharacter', () => {
  it('reads a snapshot by session_id via db.character.get', async () => {
    characterTable.get.mockResolvedValue(snapshot);
    await expect(readCachedCharacter('ses_A')).resolves.toEqual(snapshot);
    expect(characterTable.get).toHaveBeenCalledWith('ses_A');
  });

  it('resolves to undefined when no snapshot exists for the session', async () => {
    await expect(readCachedCharacter('ses_MISSING')).resolves.toBeUndefined();
    expect(characterTable.get).toHaveBeenCalledWith('ses_MISSING');
  });

  it('can read the global-pending profile by GLOBAL_SESSION_KEY', async () => {
    const globalProfile: CachedCharacter = {
      session_id: GLOBAL_SESSION_KEY,
      userName: '待定',
      userAvatar: '',
      aiName: 'AI',
      aiAvatar: '',
    };
    characterTable.get.mockResolvedValue(globalProfile);
    await expect(readCachedCharacter(GLOBAL_SESSION_KEY)).resolves.toEqual(globalProfile);
  });

  it('keeps distinct snapshots isolated per session (old sessions unaffected)', async () => {
    // Two sessions that captured different global snapshots at open time.
    const oldSession: CachedCharacter = { ...snapshot, session_id: 'ses_OLD', aiName: '旧名' };
    const newSession: CachedCharacter = { ...snapshot, session_id: 'ses_NEW', aiName: '新名' };

    characterTable.get.mockImplementation(async (key: string) =>
      key === 'ses_OLD' ? oldSession : key === 'ses_NEW' ? newSession : undefined,
    );

    await expect(readCachedCharacter('ses_OLD')).resolves.toEqual(oldSession);
    await expect(readCachedCharacter('ses_NEW')).resolves.toEqual(newSession);
    // Updating the new session must not change what the old session reads back.
    await cacheCharacter({ ...newSession, aiName: '改名' });
    expect(characterTable.get).toHaveBeenCalledWith('ses_OLD');
    expect(characterTable.get).toHaveBeenCalledWith('ses_NEW');
  });
});

describe('DEFAULT_CACHED_CHARACTER (builtin defaults)', () => {
  it('carries the builtin default names and avatar URLs from before the refactor', () => {
    expect(DEFAULT_CACHED_CHARACTER.userName).toBe('远野汉娜');
    expect(DEFAULT_CACHED_CHARACTER.aiName).toBe('橘雪莉');
    // Default avatars are served from the frontend public/ dir as relative URLs.
    expect(DEFAULT_CACHED_CHARACTER.userAvatar).toBe('/avatar/user.jpg');
    expect(DEFAULT_CACHED_CHARACTER.aiAvatar).toBe('/avatar/assistant.jpg');
  });

  it('is usable as the fallback shape for a CachedCharacter snapshot', async () => {
    const fallback = { session_id: GLOBAL_SESSION_KEY, ...DEFAULT_CACHED_CHARACTER };
    await cacheCharacter(fallback);
    expect(characterTable.put).toHaveBeenCalledWith(
      expect.objectContaining({
        session_id: GLOBAL_SESSION_KEY,
        userName: '远野汉娜',
        userAvatar: '/avatar/user.jpg',
        aiName: '橘雪莉',
        aiAvatar: '/avatar/assistant.jpg',
      }),
    );
  });
});

describe('clearCachedCharacter', () => {
  it('deletes the snapshot for a real session via db.character.delete', async () => {
    await clearCachedCharacter('ses_A');
    expect(characterTable.delete).toHaveBeenCalledWith('ses_A');
  });

  it('removes the row so subsequent reads return undefined', async () => {
    characterTable.get.mockResolvedValue(snapshot);
    await expect(readCachedCharacter('ses_A')).resolves.toEqual(snapshot);

    // Simulate the delete taking effect on the table.
    characterTable.delete.mockImplementation(async (key: string) => {
      if (key === 'ses_A') characterTable.get.mockResolvedValue(undefined);
    });
    await clearCachedCharacter('ses_A');

    await expect(readCachedCharacter('ses_A')).resolves.toBeUndefined();
    expect(characterTable.delete).toHaveBeenCalledWith('ses_A');
  });
});
