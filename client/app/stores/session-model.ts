import { defineStore } from 'pinia';
import type { SessionModelOverride, SessionModelProfile } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchSessionModel, setSessionModel } from '~/composables/bridge/session';

/** Sentinel id of the "follow the environment config" entry. */
export const ENV_MODEL_ID = 'env';

/**
 * Per-session main-model control state.
 *
 * The backend owns the truth (the session state registers); this store mirrors
 * the current session's choice for the toolbar picker and pushes changes via
 * `PUT /sessions/model`. The options come from the environment-config MAIN_LLM
 * profile list (the same list the 环境配置 panel shows), and the choice lands
 * on the NEXT turn — the backend refuses writes while a turn is in flight.
 *
 * A missing key means "not hydrated yet"; `ENV_MODEL_ID` means the session
 * follows the env-configured main LLM (the default).
 */
export const useSessionModelStore = defineStore('sessionModel', () => {
  /** sid → selected profile id (`env` = follow the env config). */
  const bySession = ref<Record<string, string>>({});
  /** sid → the stored override descriptor (display only; credential masked). */
  const overrideBySession = ref<Record<string, SessionModelOverride | null>>({});
  /** sid → the choice was parked mid-turn and lands on the next turn. */
  const pendingBySession = ref<Record<string, boolean>>({});
  /** The env-configured identity the `env` entry stands for. */
  const envModel = ref<{ provider: string | null; model: string | null }>({
    provider: null,
    model: null
  });

  /**
   * Current selection of a session: a profile id, or `env` for the default.
   * @param sessionId
   */
  function currentId(sessionId: string): string {
    return bySession.value[sessionId] ?? ENV_MODEL_ID;
  }

  /**
   * Whether the session's model choice is parked (lands on the next turn).
   * @param sessionId
   */
  function isPending(sessionId: string): boolean {
    return pendingBySession.value[sessionId] === true;
  }

  /**
   * Pull the session's persisted override and mirror it into the control.
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    const state = await fetchSessionModel(sessionId);
    envModel.value = state.env_model;
    bySession.value = { ...bySession.value, [sessionId]: state.override?.id ?? ENV_MODEL_ID };
    overrideBySession.value = { ...overrideBySession.value, [sessionId]: state.override };
    pendingBySession.value = { ...pendingBySession.value, [sessionId]: state.pending === true };
  }

  /**
   * Optimistically switch the session's model and persist; rolls back on
   * failure. Callers must not invoke this while the session is streaming.
   * @param sessionId
   * @param profile The chosen env-config profile, or null to follow the env config.
   */
  async function select(sessionId: string, profile: SessionModelProfile | null): Promise<void> {
    const previousId = bySession.value[sessionId];
    const previousOverride = overrideBySession.value[sessionId] ?? null;
    bySession.value = { ...bySession.value, [sessionId]: profile?.id ?? ENV_MODEL_ID };
    overrideBySession.value = {
      ...overrideBySession.value,
      [sessionId]: profile ? { ...profile, has_api_key: Boolean(profile.api_key) } : null
    };
    pendingBySession.value = { ...pendingBySession.value, [sessionId]: true };
    try {
      const state = await setSessionModel(sessionId, profile);
      envModel.value = state.env_model;
      bySession.value = { ...bySession.value, [sessionId]: state.override?.id ?? ENV_MODEL_ID };
      overrideBySession.value = { ...overrideBySession.value, [sessionId]: state.override };
      pendingBySession.value = { ...pendingBySession.value, [sessionId]: state.pending === true };
    } catch {
      bySession.value = {
        ...bySession.value,
        [sessionId]: previousId ?? ENV_MODEL_ID
      };
      overrideBySession.value = { ...overrideBySession.value, [sessionId]: previousOverride };
      pendingBySession.value = { ...pendingBySession.value, [sessionId]: false };
    }
  }

  return { bySession, overrideBySession, pendingBySession, envModel, currentId, hydrate, isPending, select };
});
