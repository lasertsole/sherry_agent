import { defineStore } from 'pinia';
import type { AccessMode } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchAccessMode, setAccessMode } from '~/composables/bridge/session';

/**
 * Per-session access mode (toolbar shield control).
 *
 * Three positions: ``confirm_all`` (strict — every command and every file change
 * asks), ``auto_edit`` (default — the normal approval gates) and ``full_access``
 * (bypass-all). The backend owns the truth: the modes live in the session state
 * register the approval pipeline reads on every tool call, so a switch applies
 * from the next tool call on — including a session that answered an approval card
 * with "approve all" and is switched back to a stricter mode.
 */
export const useAccessModeStore = defineStore('accessMode', () => {
  /** sid → mode (absent until the first successful read). */
  const bySession = ref<Record<string, AccessMode>>({});

  /**
   * Current mode of a session (defaults to ``auto_edit``).
   * @param sessionId
   */
  function modeFor(sessionId: string): AccessMode {
    return bySession.value[sessionId] ?? 'auto_edit';
  }

  /**
   * Pull the session's mode from the backend (a failed read keeps the last one).
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    try {
      const mode = await fetchAccessMode(sessionId);
      bySession.value = { ...bySession.value, [sessionId]: mode };
    } catch {
      // Keep what we have; the next hydrate retries.
    }
  }

  /**
   * Switch the session's mode and mirror the applied value.
   * @param sessionId
   * @param mode
   */
  async function select(sessionId: string, mode: AccessMode): Promise<void> {
    const previous = modeFor(sessionId);
    bySession.value = { ...bySession.value, [sessionId]: mode };
    try {
      const applied = await setAccessMode(sessionId, mode);
      bySession.value = { ...bySession.value, [sessionId]: applied };
    } catch {
      bySession.value = { ...bySession.value, [sessionId]: previous };
    }
  }

  /**
   * Mirror a bypass-all the BACKEND already applied: answering an approval card
   * with "approve all" sets the session's YOLO flag server-side, so the control
   * must show 完全访问 immediately instead of waiting for a reload. Nothing is
   * written here — the card's decision is what sets the flag.
   * @param sessionId
   */
  function markYolo(sessionId: string): void {
    if (!sessionId) return;
    bySession.value = { ...bySession.value, [sessionId]: 'full_access' };
  }

  return { bySession, modeFor, hydrate, select, markYolo };
});
