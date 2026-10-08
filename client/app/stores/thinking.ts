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
  /** sid → the choice was parked mid-turn and lands on the next turn. */
  const pendingBySession = ref<Record<string, boolean>>({});
  /** What an UNSET on/off control follows (the env flag the backend reports). */
  const defaultEnabled = ref(false);
  /** What an UNSET level control follows. */
  const defaultLevel = ref<'low' | 'high' | 'max'>('high');

  /**
   * The position an unset control follows (until the first hydrate says more).
   * @returns The mode's default position (off / high before the first read).
   */
  function unsetValue(): ThinkingValue {
    return mode.value === 'levels' ? defaultLevel.value : defaultEnabled.value;
  }

  /**
   * Current control position for a session, falling back to the backend's
   * reported default while the session is not hydrated yet.
   * @param sessionId
   */
  function current(sessionId: string): ThinkingValue {
    return bySession.value[sessionId] ?? unsetValue();
  }

  /**
   * Pull the session's persisted choice and mirror it into the control.
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    const state = await fetchThinkingState(sessionId);
    mode.value = state.mode;
    defaultEnabled.value = state.defaultEnabled === true;
    defaultLevel.value = state.defaultLevel ?? 'high';
    // Unset follows the BACKEND's default (the env flag / level default it
    // reports): a hardcoded `false` here showed "off" for a session whose model
    // was still thinking, so the switch looked ignored.
    const value: ThinkingValue =
      state.mode === 'levels'
        ? (state.level ?? state.defaultLevel ?? 'high')
        : (state.enabled ?? state.defaultEnabled ?? false);
    bySession.value = { ...bySession.value, [sessionId]: value };
    pendingBySession.value = { ...pendingBySession.value, [sessionId]: state.pending === true };
  }

  /**
   * Whether the session's choice is parked (lands on the next turn).
   * @param sessionId
   */
  function isPending(sessionId: string): boolean {
    return pendingBySession.value[sessionId] === true;
  }

  /**
   * Optimistically move the control and persist; rolls back on failure.
   * Callers must not invoke this while the session is streaming.
   * @param sessionId
   * @param value
   */
  async function setValue(sessionId: string, value: ThinkingValue): Promise<void> {
    const previous = bySession.value[sessionId];
    const fallback: ThinkingValue = previous ?? unsetValue();
    const previousPending = pendingBySession.value[sessionId] ?? false;
    bySession.value = { ...bySession.value, [sessionId]: value };
    // Optimistically "parked" until the server says the write landed live.
    pendingBySession.value = { ...pendingBySession.value, [sessionId]: true };
    try {
      const result = await setThinkingValue(sessionId, value);
      pendingBySession.value = { ...pendingBySession.value, [sessionId]: result.pending };
    } catch {
      bySession.value = { ...bySession.value, [sessionId]: fallback };
      pendingBySession.value = { ...pendingBySession.value, [sessionId]: previousPending };
    }
  }

  return {
    mode,
    bySession,
    pendingBySession,
    defaultEnabled,
    defaultLevel,
    current,
    hydrate,
    isPending,
    setValue
  };
});
