import { defineStore } from 'pinia';
import type { SearchBackend, SearchBackendState } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchSearchBackend, setSearchBackend } from '~/composables/bridge/session';

/** One backend downgrade the backend reported over the socket. */
export interface SearchFallbackNotice {
  /** The backend now serving searches. */
  backend: SearchBackend;
  /** Why the switch happened: `tavily_auth_failed` / `tavily_timeout` / `tavily_error`. */
  reason: string;
  /** The backend's own one-line message (rendered only when the reason is unknown). */
  message: string;
  /** Arrival time (ms) — kept so a reload/switch can order several notices. */
  at: number;
}

/**
 * Web-search state: the 模型联网 panel's per-session backend choice, and the
 * downgrade notices the tool pushes while it runs.
 *
 * The backend owns the truth (the session state register + the .env providers);
 * this store mirrors the choice for the panel and holds the transient fallback
 * banner until the user dismisses it. The `search_backend_fallback` frame is
 * best-effort — a missed frame costs the banner, never the search, whose result
 * text names the backend it came from.
 */
export const useWebSearchStore = defineStore('webSearch', () => {
  /** sid → last read state (absent until the first successful read). */
  const stateBySession = ref<Record<string, SearchBackendState>>({});
  /** sid → a read is in flight (keeps concurrent `init()` calls from double-firing). */
  const hydratingBySession = ref<Record<string, boolean>>({});
  /** sid → the undismissed downgrade notice (cleared by `dismissFallback`). */
  const fallbackBySession = ref<Record<string, SearchFallbackNotice>>({});
  /** Singleton guard: the socket listener is registered exactly once. */
  const subscribed = ref(false);

  /**
   * The session's known state, or null before the first read.
   * @param sessionId
   */
  function stateFor(sessionId: string): SearchBackendState | null {
    return stateBySession.value[sessionId] ?? null;
  }

  /**
   * The session's undismissed downgrade notice, or null.
   * @param sessionId
   */
  function fallbackFor(sessionId: string): SearchFallbackNotice | null {
    return fallbackBySession.value[sessionId] ?? null;
  }

  /**
   * Pull the session's backend state (a failed read keeps the last one).
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    hydratingBySession.value = { ...hydratingBySession.value, [sessionId]: true };
    try {
      const state = await fetchSearchBackend(sessionId);
      stateBySession.value = { ...stateBySession.value, [sessionId]: state };
    } catch {
      // Keep what we have; the next hydrate retries.
    } finally {
      hydratingBySession.value = { ...hydratingBySession.value, [sessionId]: false };
    }
  }

  /**
   * Choose the session's backend and mirror what the backend stored; rolls back
   * on failure. ``null`` follows the environment default again.
   * @param sessionId
   * @param backend
   */
  async function select(sessionId: string, backend: SearchBackend | null): Promise<void> {
    const previous = stateBySession.value[sessionId] ?? null;
    if (previous) {
      // Optimistic: the radio answers at once; the effective backend keeps the
      // old value until the reply says how availability resolved the new one.
      stateBySession.value = {
        ...stateBySession.value,
        [sessionId]: {
          ...previous,
          backend,
          pending: true,
          pending_value: backend,
          // An explicit pick IS what the tool will prefer unless it is unconfigured.
          effective:
            backend ??
            (previous.tavily_available ? 'tavily' : previous.searxng.configured ? 'searxng' : previous.effective)
        }
      };
    }
    try {
      const applied = await setSearchBackend(sessionId, backend);
      const current = stateBySession.value[sessionId];
      if (current) {
        stateBySession.value = {
          ...stateBySession.value,
          [sessionId]: { ...current, backend: applied.backend, pending: applied.pending }
        };
      } else {
        await hydrate(sessionId);
      }
    } catch {
      if (previous) {
        stateBySession.value = { ...stateBySession.value, [sessionId]: previous };
      }
    }
  }

  /**
   * Show a downgrade notice for a session (the `search_backend_fallback`
   * frame). A later notice replaces an undismissed earlier one — the banner
   * describes the CURRENT backend, and two stacked banners would say the same.
   * @param payload Raw frame payload (`{session_id, content: {backend, reason, message}}`).
   */
  function noteFallback(payload: unknown): void {
    const frame = payload as
      { session_id?: string; content?: { backend?: unknown; reason?: unknown; message?: unknown } } | null | undefined;
    const sessionId = String(frame?.session_id ?? '');
    if (!sessionId) return;
    const backend: SearchBackend = frame?.content?.backend === 'tavily' ? 'tavily' : 'searxng';
    fallbackBySession.value = {
      ...fallbackBySession.value,
      [sessionId]: {
        backend,
        reason: String(frame?.content?.reason ?? ''),
        message: String(frame?.content?.message ?? ''),
        at: Date.now()
      }
    };
  }

  /**
   * Drop the session's notice (the banner's own dismiss action).
   * @param sessionId
   */
  function dismissFallback(sessionId: string): void {
    if (!sessionId) return;
    const rest = { ...fallbackBySession.value };
    delete rest[sessionId];
    fallbackBySession.value = rest;
  }

  /** Register the socket listener once (singleton guard, like the todo store). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:search_backend_fallback', noteFallback);
  }

  /**
   * Initialise for a session: install the listener once and pull the state.
   * Idempotent; safe to call from every consumer's `onMounted`.
   * @param sessionId
   */
  function init(sessionId: string): void {
    subscribe();
    if (sessionId && !stateBySession.value[sessionId] && !hydratingBySession.value[sessionId]) {
      void hydrate(sessionId);
    }
  }

  return {
    stateBySession,
    fallbackBySession,
    stateFor,
    fallbackFor,
    hydrate,
    select,
    noteFallback,
    dismissFallback,
    subscribe,
    init
  };
});
