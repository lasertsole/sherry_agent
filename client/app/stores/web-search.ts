import { defineStore } from 'pinia';
import type { SearchBackend, SearchBackendState } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchSearchBackend, setSearchBackend } from '~/composables/bridge/session';

/**
 * The localized one-line downgrade notice. Built here because the frame arrives
 * on a non-component path (a store action), where `useI18n` is unavailable: the
 * runtime resolver falls back to the frame's own message when i18n is not
 * mounted (unit tests, a frame before boot).
 * @param backend The backend now serving searches.
 * @param reason The reason code the tool sent.
 * @param message The backend's own message (the fallback text).
 * @returns The notice to show in 通知.
 */
export function fallbackNoticeText(backend: SearchBackend, reason: string, message: string): string {
  const t = resolveRuntimeT();
  if (!t) return message || `${backend} (${reason})`;
  const reasonKey = `searchFallback.reason.${reason}`;
  const translated = t(reasonKey);
  // A known reason is translated, anything else (a future code) is shown verbatim.
  const reasonText = translated === reasonKey ? reason : translated;
  const text = t('searchFallback.notice', {
    reason: reasonText,
    name: backend === 'searxng' ? 'SearXNG' : 'Tavily'
  });
  // An unresolved key comes back as the key itself (i18n has no such message).
  return text === 'searchFallback.notice' ? message || `${backend} (${reason})` : text;
}

/**
 * Web-search state: the 模型联网 panel's per-session backend choice, plus the
 * downgrade the tool reports while it runs.
 *
 * The backend owns the truth (the session state register + the .env providers);
 * this store mirrors the choice for the panel and turns the
 * `search_backend_fallback` frame into a 通知 entry — that is where an operator
 * looks for "something changed under me", and the badge keeps counting while
 * the panel is closed. The frame is best-effort: a missed one costs the notice,
 * never the search, whose result text names the backend it came from.
 */
export const useWebSearchStore = defineStore('webSearch', () => {
  /** sid → last read state (absent until the first successful read). */
  const stateBySession = ref<Record<string, SearchBackendState>>({});
  /** sid → a read is in flight (keeps concurrent `init()` calls from double-firing). */
  const hydratingBySession = ref<Record<string, boolean>>({});
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
   * Turn the `search_backend_fallback` frame into a 通知 entry. Identical
   * consecutive downgrades merge into one counted item (the notification
   * store's own rule), so a burst of retries reads as one line.
   * @param payload Raw frame payload (`{session_id, content: {backend, reason, message}}`).
   */
  function handleFallbackFrame(payload: unknown): void {
    const frame = payload as
      { session_id?: string; content?: { backend?: unknown; reason?: unknown; message?: unknown } } | null | undefined;
    if (!String(frame?.session_id ?? '')) return;
    const backend: SearchBackend = frame?.content?.backend === 'tavily' ? 'tavily' : 'searxng';
    const reason = String(frame?.content?.reason ?? '');
    const text = fallbackNoticeText(backend, reason, String(frame?.content?.message ?? ''));
    useNotificationStore().handleNotification(text, 'web_search');
  }

  /** Register the socket listener once (singleton guard, like the todo store). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:search_backend_fallback', handleFallbackFrame);
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
    hydratingBySession,
    stateFor,
    hydrate,
    select,
    handleFallbackFrame,
    subscribe,
    init
  };
});
