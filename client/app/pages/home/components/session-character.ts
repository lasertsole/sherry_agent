/**
 * Session character snapshot helpers (extracted from SessionSidebar.vue).
 *
 * The session-list nav panel needs `ensureSessionCharacter`; importing it back
 * from SessionSidebar.vue would create a cycle (nav store → panel → shell → nav
 * store). Keeping it in a plain module lets the nav store import the panel
 * synchronously without that cycle.
 */
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  DEFAULT_CACHED_CHARACTER,
  GLOBAL_SESSION_KEY,
  cacheCharacter,
  readCachedCharacter,
  type CachedCharacter
} from '@/composables/db';
import { logUtil } from '~/utils/log';

/** The neutral fallback character, built field by field from the defaults. */
const defaultCharacter = (): Pick<CachedCharacter, 'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'> => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

/**
 * Ensure a session has locked its own character snapshot.
 * @param sessionId Session ID
 */
export async function ensureSessionCharacter(sessionId: string): Promise<void> {
  try {
    const [globalSnap, sessionSnap] = await Promise.all([
      readCachedCharacter(GLOBAL_SESSION_KEY),
      readCachedCharacter(sessionId)
    ]);
    if (sessionSnap) return;
    const base = globalSnap ?? defaultCharacter();
    const locked: CachedCharacter = { ...base, session_id: sessionId };
    await cacheCharacter(locked);
  } catch (error) {
    logUtil.w('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
}
