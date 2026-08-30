import { ref, type Ref } from 'vue';
import { isSessionWsOpen, useWs } from '~/composables/ws';
import { on, off } from '~/composables/mitt';
import { resolveRuntimeT } from '~/composables/i18nRuntime';
import { toastInfo, toastWarn } from '~/composables/toast';

/**
 * Network / backend connectivity monitoring (event-driven, based on WebSocket heartbeat liveness detection).
 *
 * Responsibilities:
 *  1. Ensure the `/sessions/ws` session-push singleton is established (ws.ts has a built-in 10s
 *     application-layer ping/pong heartbeat + 5s auto-reconnect) and subscribe to its mitt events
 *     to reflect backend reachability:
 *       - `ws:connected`      -> backendStatus = 'ok' (info toast when recovering from unavailable)
 *       - `ws:disconnected`   -> backendStatus = 'down' (warn toast on first failure)
 *  2. Listen to the browser's online/offline events to maintain isOnline (on offline, also mark the
 *     backend unreachable per the rules below; on online, do not optimistically mark ok — the
 *     WS reconnect events make that call).
 *  3. Pop global toasts on state changes (all deduplicated via the lastReachable "edge": within a
 *     reconnect loop, ws:disconnected firing repeatedly every 5s never re-triggers a toast):
 *       - Network lost        -> warn  `connection.offline`
 *       - Backend unreachable -> warn  `connection.backendDown`
 *       - Recovered           -> info  `connection.backOnline`
 *
 * Timeliness semantics:
 *  - Backend process crash (TCP drops immediately): the banner appears within ~ms–5s (onclose ->
 *    ws:disconnected); suspended process (TCP stays open, frames unanswered): appears within
 *    ~15–30s (two consecutive pong timeouts = 10s heartbeat interval + 5s timeout window × 2).
 *
 * Design constraints:
 *  - Every exported function is a safe no-op when `import.meta.client === false` or when monitoring has not started.
 *  - stopConnectionWatch only removes this module's mitt subscriptions and window listeners; it never
 *    closes the WS singleton (other consumers such as NotificationDialog still use the same connection).
 *  - The i18n `t` falls back safely to returning the key unchanged outside Nuxt context.
 */

/**
 * Test-only: explicitly override the client-semantics flag (never call from production code).
 * Background: Vitest's import.meta lacks Nuxt's client/server semantics (undefined → falsy),
 * so tests must inject it explicitly.
 * Key implementation constraint: production code must reference the **literal** `import.meta.client` —
 * Nuxt/Vite's build-time static replacement only applies to that literal expression; if accessed
 * through an alias such as `const meta = import.meta` then `meta.client`, the aliased property does
 * not exist at runtime (undefined → always falsy) and every client guard silently breaks
 * (pitfall confirmed by 2026-08 E2E testing: connectivity monitoring never started in the browser and the banner never appeared).
 */
let clientFlagOverride: boolean | null = null;

export function _setClientFlag(client: boolean): void {
  clientFlagOverride = client;
}

/** Whether we are currently in a browser client environment (production uses build-time static replacement; tests use the explicit override). */
function isClient(): boolean {
  if (clientFlagOverride !== null) return clientFlagOverride;
  return import.meta.client === true;
}

/** Network online state (initialized from the browser's navigator.onLine; assumed online until proven otherwise). */
export const isOnline: Ref<boolean> = ref(true);

/** Backend service connection status: 'unknown' | 'ok' | 'down'. */
export type BackendStatus = 'unknown' | 'ok' | 'down';

/** Backend health status. */
export const backendStatus: Ref<BackendStatus> = ref('unknown');

/** Whether monitoring has started (idempotence flag: prevents duplicate mitt / window event subscriptions). */
const watching = ref(false);

/** Last observed overall reachability state (network loss and backend-unreachable share the same
 *  "edge" determination, used to deduplicate toasts and trigger the "recovered" toast). */
let lastReachable: boolean | null = null;

/** i18n keys, corresponding to connection.* in locales/*.json. */
const OFF_LINE_KEY = 'connection.offline';
const BACKEND_DOWN_KEY = 'connection.backendDown';
const BACK_ONLINE_KEY = 'connection.backOnline';

/**
 * Safely get an i18n translation.
 *
 * Delegates to `resolveRuntimeT()` (i18nRuntime.ts) to resolve the real translation function at
 * Nuxt runtime (nuxt-i18n v10's `$i18n` is a locale state proxy without `t` and cannot be used directly);
 * unit tests / non-Nuxt contexts fall back to returning the key unchanged. Never throws in either case.
 */
function safeT(key: string): string {
  if (!isClient()) return key;
  const t = resolveRuntimeT();
  return t ? t(key) : key;
}

/** Sync the browser online state once (browser environment). */
function syncBrowserOnline(): void {
  if (!isClient()) return;
  const onLine = typeof navigator !== 'undefined' ? navigator.onLine : true;
  isOnline.value = onLine === true;
}

/**
 * WS connection established (ws:connected): mark the backend as ok.
 * If it was previously unavailable (network lost / backend unreachable), pop the "recovered" info toast;
 * lastReachable edge deduplication — neither the first connection (null -> true) nor already-ok (true -> true) pops a toast.
 */
function handleWsUp(): void {
  backendStatus.value = 'ok';
  if (lastReachable === false) {
    // Was unavailable before, now recovered
    toastInfo(safeT(BACK_ONLINE_KEY));
  }
  lastReachable = true;
}

/**
 * WS connection lost (ws:disconnected): mark the backend as down.
 * When the browser is offline (navigator.onLine === false), also set isOnline = false;
 * the toast text distinguishes network loss / backend-unreachable; lastReachable edge deduplication
 * ensures the disconnect event arriving repeatedly every 5s within a reconnect loop never re-pops a toast.
 */
function handleWsDown(): void {
  const offLine = typeof navigator !== 'undefined' && navigator.onLine === false;
  if (offLine) {
    isOnline.value = false;
  }
  backendStatus.value = 'down';
  if (lastReachable !== false) {
    lastReachable = false;
    toastWarn(safeT(offLine ? OFF_LINE_KEY : BACKEND_DOWN_KEY));
  }
}

/** Browser back online (window online event): only sync isOnline.
 *  Do not optimistically mark the backend ok — the connection has not been rebuilt yet;
 *  the subsequent ws:connected event makes that call. */
function handleBrowserOnline(): void {
  syncBrowserOnline();
}

/** Browser offline (window offline event): sync isOnline and mark the backend unreachable following
 *  handleWsDown's edge rules (first trigger pops the connection.offline warn toast). */
function handleBrowserOffline(): void {
  syncBrowserOnline();
  handleWsDown();
}

/**
 * Manually re-sync connectivity state once (no network requests).
 *
 * Purely local sync: copy navigator.onLine into isOnline, then read the live readyState of the
 * /sessions/ws singleton to set backendStatus. No toasts are popped (toasts are left to the
 * WS events' edge logic as the single arbiter). Used by tests and external callers that need to
 * refresh the state manually.
 */
export async function checkConnectivity(): Promise<void> {
  if (!isClient()) return;

  syncBrowserOnline();

  // Read the WS singleton's live connection state (no network request): OPEN -> ok, otherwise down
  backendStatus.value = isSessionWsOpen() ? 'ok' : 'down';
}

/**
 * Start connectivity monitoring (event-driven).
 *
 * @returns Stop handle; calling it removes this module's mitt subscriptions and window listeners (does not close the WS singleton).
 */
export function startConnectionWatch(): () => void {
  // Not a client: return an empty stop function without starting any monitoring
  if (!isClient()) {
    return () => {};
  }

  // Already started: return the same stop handle (idempotent, no duplicate subscriptions)
  if (watching.value) {
    return () => stopConnectionWatch();
  }

  watching.value = true;

  // 1) Ensure the /sessions/ws singleton exists and its heartbeat / auto-reconnect loop is running.
  //    Do not depend on components like NotificationDialog to establish the connection first, and do
  //    not call closeWs() here (the singleton lifecycle belongs to ws.ts; closing it would break other consumers).
  const singleton = useWs();

  // 2) Subscribe to the WS connection events (referenced in pairs, removed by stopConnectionWatch)
  on('ws:connected', handleWsUp);
  on('ws:disconnected', handleWsDown);

  // 3) Browser network events: offline -> network-loss handling; online -> only sync isOnline
  window.addEventListener('online', handleBrowserOnline);
  window.addEventListener('offline', handleBrowserOffline);

  // 4) Initial state convergence: if the singleton is already connected (an existing connection will
  //    not emit ws:connected again), mark ok immediately; otherwise stay 'unknown' and let the first
  //    WS event make the call.
  if (singleton.isConnected.value === true || singleton.ws.value?.readyState === WebSocket.OPEN) {
    handleWsUp();
  }

  return () => stopConnectionWatch();
}

/**
 * Stop connectivity monitoring and remove subscriptions. Idempotent.
 *
 * Note: this only removes this module's mitt subscriptions and window listeners; it does **not**
 * close the /sessions/ws singleton — that connection is shared application-wide (NotificationDialog
 * and others also consume it), and closing it would break them.
 */
export function stopConnectionWatch(): void {
  if (!watching.value) {
    return;
  }
  off('ws:connected', handleWsUp);
  off('ws:disconnected', handleWsDown);
  window.removeEventListener('online', handleBrowserOnline);
  window.removeEventListener('offline', handleBrowserOffline);
  watching.value = false;
}

/**
 * Test-only: reset the module-level singleton's private state (test isolation, never call from production code).
 * The key is `lastReachable` — it drives the "failure / recovered toast" edge determination; if it
 * leaks across test cases, a down state left by a previous case makes the next case's failure be
 * misjudged as "recovered" (or vice versa, missing a toast).
 */
export function _resetStateForTest(): void {
  isOnline.value = true;
  backendStatus.value = 'unknown';
  lastReachable = null;
  stopConnectionWatch();
}

/**
 * Composable entry point: returns connectivity-related reactive state and lifecycle controls.
 *
 * Meant to be called from app.vue / components inside setup; it reuses the module-level singleton
 * state so the whole application shares the same isOnline / backendStatus.
 */
export function useConnection() {
  return {
    isOnline,
    backendStatus,
    startConnectionWatch,
    stopConnectionWatch,
    checkConnectivity
  };
}
