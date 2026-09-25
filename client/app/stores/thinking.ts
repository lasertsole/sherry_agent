import { defineStore } from 'pinia';
import type { ThinkingMode, ThinkingValue } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchThinkingState, setThinkingValue } from '~/composables/bridge/session';

/**
 * Per-session model thinking control state.
 *
 * The backend owns the truth (session state registers); this store mirrors the
 * current session's explicit choice for the toolbar control and pushes changes
 * via `PUT /sessions/thinking`. An absent key means "not hydrated yet". The
 * control mode is a property of the configured MODEL (process-global): switch
 * models use a boolean, always-think gateways use a 低/高/最高 selector.
 */
export const useThinkingStore = defineStore('thinking', () => {
  /** The configured model's control mode (hydrated with the first session). */
  const mode = ref<ThinkingMode>('on_off');
  /** sid → explicit user choice (hydrated from the backend or set locally). */
  const bySession = ref<Record<string, ThinkingValue>>({});

  /**
   * Current control position for a session (off / high until hydrated —
   * matching the server default of thinking enabled at its default level).
   * @param sessionId
   */
  function current(sessionId: string): ThinkingValue {
    if (mode.value === 'levels') return bySession.value[sessionId] ?? 'high';
    return bySession.value[sessionId] ?? false;
  }

  /**
   * Pull the session's persisted choice and mirror it into the control.
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    const state = await fetchThinkingState(sessionId);
    mode.value = state.mode;
    const value: ThinkingValue = state.mode === 'levels' ? (state.level ?? 'high') : (state.enabled ?? false);
    bySession.value = { ...bySession.value, [sessionId]: value };
  }

  /**
   * Optimistically move the control and persist; rolls back on failure.
   * Callers must not invoke this while the session is streaming.
   * @param sessionId
   * @param value
   */
  async function setValue(sessionId: string, value: ThinkingValue): Promise<void> {
    const previous = bySession.value[sessionId];
    const fallback: ThinkingValue = previous ?? (mode.value === 'levels' ? 'high' : false);
    bySession.value = { ...bySession.value, [sessionId]: value };
    try {
      await setThinkingValue(sessionId, value);
    } catch {
      bySession.value = { ...bySession.value, [sessionId]: fallback };
    }
  }

  return { mode, bySession, current, hydrate, setValue };
});
