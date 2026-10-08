import { isClient } from '~/utils/client';

/**
 * Safely get an i18n translation from a non-setup context (event callbacks,
 * timers, store actions, module-level helpers).
 *
 * Delegates to `resolveRuntimeT()` (i18nRuntime.ts, Nuxt auto-import) to resolve
 * the real translation function within the Nuxt runtime — nuxt-i18n v10's
 * `$i18n` is a locale state proxy that does not include `t`, so it cannot be
 * used directly. Unit tests / non-Nuxt contexts (and a missing composer) fall
 * back to returning the key as-is. Never throws in either case.
 *
 * Shared by `toast.ts` and `stores/connection.ts`, which previously carried
 * byte-identical private copies.
 * @param key i18n message key
 * @returns The translated message, or the key unchanged when no translator is available
 */
export function safeT(key: string): string {
  if (!isClient()) return key;
  const t = resolveRuntimeT();
  return t ? t(key) : key;
}
