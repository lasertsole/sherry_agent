import type { ToastMessageOptions } from 'primevue/toast';
import type { ToastServiceMethods } from 'primevue/toastservice';
import { isClient } from '~/utils/client';
import { safeT } from '~/utils/i18n';

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
 *  - The shared client-flag guard (and its `_setClientFlag` test override) lives in
 *    `~/utils/client`; it is re-exported here so existing toast tests keep importing it
 *    from this module.
 */

export { _setClientFlag } from '~/utils/client';

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
 * Unified dispatch entry: silently returns when unregistered / not on the client.
 * @param message
 */
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
