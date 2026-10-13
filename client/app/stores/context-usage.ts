import { defineStore } from 'pinia';
import type { ContextUsage } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchContextUsage } from '~/composables/bridge/session';

/**
 * Per-session context-window accounting (chat toolbar's usage ring).
 *
 * The backend owns the numbers: it knows the configured window, the prompt size
 * the provider reported for the last turn, and the parts it can estimate itself
 * (system prompt, tool schemas). The store mirrors them per session and
 * refreshes when a turn finishes or the popover opens.
 */
export const useContextUsageStore = defineStore('contextUsage', () => {
  /** sid → last known accounting (absent until the first successful read). */
  const bySession = ref<Record<string, ContextUsage>>({});

  /**
   * Pull the session's accounting from the backend (no-op for an empty id).
   * A failed read keeps the previous numbers: the ring must not blink out.
   * @param sessionId
   */
  async function refresh(sessionId: string): Promise<void> {
    if (!sessionId) return;
    try {
      const usage = await fetchContextUsage(sessionId);
      bySession.value = { ...bySession.value, [sessionId]: usage };
    } catch {
      // Keep what we have; the next refresh retries.
    }
  }

  /**
   * Last known accounting of a session, or null before the first read.
   * @param sessionId
   */
  function usageFor(sessionId: string): ContextUsage | null {
    return bySession.value[sessionId] ?? null;
  }

  return { bySession, refresh, usageFor };
});
