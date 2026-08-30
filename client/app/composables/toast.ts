import type { ToastMessageOptions } from 'primevue/toast';
import type { ToastServiceMethods } from 'primevue/toastservice';
import { resolveRuntimeT } from '~/composables/i18nRuntime';

/**
 * Global toast notification layer.
 *
 * Design constraints:
 *  - Do not import `useToast` (Nuxt auto-import / PrimeVue composable) at the module
 *    top level, because the unit-test (bare vitest) environment lacks that auto-import.
 *    Instead, `app.vue` injects the real `useToast()` result via `registerToastApi`
 *    during setup.
 *  - All exported functions are safe no-ops when "unregistered" or when
 *    `import.meta.client === false`; toast logic must never break the request chain.
 *  - The i18n `t` function safely falls back to returning the key as-is in non-Nuxt
 *    contexts (including unit tests), without throwing.
 */

/**
 * For tests only: explicitly override the client-semantics flag (do not call in
 * production code).
 * Background: Vitest's `import.meta` lacks Nuxt's client/server semantics
 * (undefined → falsy), so tests must inject this explicitly.
 * Key implementation constraint: production runtime must use the **literal**
 * `import.meta.client` — Nuxt/Vite's build-time static replacement only applies to
 * that literal expression. If accessed through an alias like `const meta = import.meta`
 * and then `meta.client`, the aliased property does not exist at runtime
 * (undefined → always falsy), and every client guard silently fails
 * (pitfall confirmed by 2026-08 E2E testing: toast registration/display was a
 * complete no-op in the browser).
 */
let clientFlagOverride: boolean | null = null;

export function _setClientFlag(client: boolean): void {
  clientFlagOverride = client;
}

/** Whether we are currently in a browser client environment (production uses build-time static replacement; tests use explicit override). */
function isClient(): boolean {
  if (clientFlagOverride !== null) return clientFlagOverride;
  return import.meta.client === true;
}

/** The ToastServiceMethods returned by useToast(); we only care about .add(...). */
type ToastApi = Pick<ToastServiceMethods, 'add'>;

let toastApi: ToastApi | null = null;

/**
 * Register the global toast instance. Called by app.vue (client setup).
 * Does not register when not on the client or when the argument is empty
 * (stays a no-op).
 *
 * @param api The value returned by useToast(); pass null/undefined to unregister
 *   (back to no-op).
 */
export function registerToastApi(api: ToastApi | null): void {
  if (!isClient()) return;
  toastApi = api;
}

/**
 * Safely get an i18n translation.
 *
 * Delegates to `resolveRuntimeT()` (i18nRuntime.ts) to resolve the real translation
 * function within the Nuxt runtime (nuxt-i18n v10's `$i18n` is a locale state proxy
 * that does not include `t`, so it cannot be used directly);
 * unit tests / non-Nuxt contexts fall back to returning the key as-is.
 * Never throws in either case.
 */
function safeT(key: string): string {
  if (!isClient()) return key;
  const t = resolveRuntimeT();
  return t ? t(key) : key;
}

/** Unified dispatch entry: silently returns when unregistered / not on the client. */
function show(message: ToastMessageOptions): void {
  if (!isClient() || !toastApi) return;
  toastApi.add(message);
}

/**
 * info-level toast.
 * @param summary Title (already translated)
 * @param detail  Body (optional)
 * @param life    Display duration (ms, default 3000)
 */
export function toastInfo(summary?: string, detail?: string, life = 3000): void {
  show({ severity: 'info', summary, detail, life });
}

/**
 * success-level toast.
 * @param summary Title (already translated)
 * @param detail  Body (optional)
 * @param life    Display duration (ms, default 3000)
 */
export function toastSuccess(summary?: string, detail?: string, life = 3000): void {
  show({ severity: 'success', summary, detail, life });
}

/**
 * warn-level toast.
 * @param summary Title (already translated)
 * @param detail  Body (optional)
 * @param life    Display duration (ms, default 5000)
 */
export function toastWarn(summary?: string, detail?: string, life = 5000): void {
  show({ severity: 'warn', summary, detail, life });
}

/**
 * error-level toast.
 * @param summary Title (already translated)
 * @param detail  Body (optional)
 * @param life    Display duration (ms, default 8000)
 */
export function toastError(summary?: string, detail?: string, life = 8000): void {
  show({ severity: 'error', summary, detail, life });
}

/** Fallback message key for request failures (corresponds to errors.requestFailed in locales/*.json). */
const REQUEST_FAILED_KEY = 'errors.requestFailed';

/**
 * Toast shown uniformly when a request fails; called by requestApi.ts after a fetch
 * failure (guarantees at most one toast per request). The summary uses the safely
 * translated `errors.requestFailed` key.
 *
 * @param detail Additional failure reason (optional)
 */
export function sendRequestErrorToast(detail?: string): void {
  const summary = safeT(REQUEST_FAILED_KEY);
  toastError(summary, detail);
}
