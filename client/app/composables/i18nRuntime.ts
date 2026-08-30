/**
 * i18n translation function resolver for non-setup contexts.
 *
 * Background (verified in-browser, 2026-08; two layers of pitfalls):
 *  1. @nuxtjs/i18n v10.6 replaces `nuxtApp.$i18n` / `globalProperties.$i18n` with a
 *     **proxy containing only locale state** (no `t`, no `global`, empty
 *     `Object.keys`), contradicting the official docs' "$i18n is the global Composer".
 *  2. The real vue-i18n instance lives under vue-i18n's own injection key
 *     `Symbol(vue-i18n)`; however, this project's Vite build (@nuxtjs/i18n's built-in
 *     @intlify/unplugin-vue-i18n alias-redirects `'vue-i18n'`) makes business code
 *     import a dist entry that **does not export `I18nInjectionKey`** (verified at
 *     runtime via dynamic import: the value is `undefined`); the nuxt-i18n plugin and
 *     business code each hold their own vue-i18n module instance, so any
 *     symbol-identity approach is unreliable.
 *
 * Resolution order:
 *  1. **Shape scan** of `vueApp._context.provides`: look for an injected value shaped
 *     like `{ global: { t: fn } }` (i.e. the vue-i18n instance) — independent of
 *     symbol/entry identity, the only reliable path under the current build
 *  2. `nuxtApp.$i18n.global.t` (older nuxt-i18n / official-documented behavior, kept
 *     for backward compatibility)
 *  3. Neither available → `undefined` (callers fall back to returning the key as-is)
 *
 * Unit tests / non-Nuxt contexts: `useNuxtApp` does not exist (ReferenceError) →
 * swallowed by try/catch, returning `undefined`; never throws. In a setup context use
 * `useI18n().t` directly (see errorCaptured.ts / useSubagentTasks.ts usage); do not
 * use this function there.
 */

/** Minimal structure of the vue-i18n global composer (this module only cares about t). */
interface MinimalComposer {
  t: (key: string) => string;
}

/** Minimal structure of a vue-i18n instance (createI18n return value): global holds the global composer. */
interface MinimalI18nInstance {
  global?: MinimalComposer;
}

/** Legacy nuxt-i18n shape: $i18n carries global directly (v9 and documented behavior). */
interface MinimalNuxtI18n {
  global?: MinimalComposer;
}

/** Runtime translation function (only single-key translation semantics are guaranteed). */
export type RuntimeT = (key: string) => string;

/**
 * Find the vue-i18n global composer by shape among the app-level provides.
 *
 * Reads key by key and tolerates getters that throw (provides may contain arbitrary
 * third-party injected values).
 */
function findGlobalComposer(provides: Record<PropertyKey, unknown>): MinimalComposer | undefined {
  for (const key of Reflect.ownKeys(provides)) {
    let value: unknown;
    try {
      value = provides[key];
    } catch {
      continue;
    }
    if (!value || typeof value !== 'object') continue;
    const candidate = (value as MinimalI18nInstance).global;
    if (candidate && typeof candidate === 'object' && typeof candidate.t === 'function') {
      return candidate;
    }
  }
  return undefined;
}

/**
 * Resolve a translation function usable in **any runtime context** (event callbacks,
 * timers, non-component modules).
 *
 * @returns A usable t function; `undefined` when Nuxt is unavailable or i18n is not
 *   mounted.
 */
export function resolveRuntimeT(): RuntimeT | undefined {
  try {
    const nuxtApp = useNuxtApp();

    // 1) Shape-scan provides (the only reliable path under the current build; see module comment)
    const composer = findGlobalComposer(nuxtApp.vueApp._context.provides);
    if (composer) return key => composer.t(key);

    // 2) Legacy-shape fallback: $i18n.global.t (nuxt-i18n v9 and documented behavior)
    const $i18n = nuxtApp?.$i18n;
    const legacyT = ($i18n as MinimalNuxtI18n | undefined)?.global?.t;
    if (typeof legacyT === 'function') return key => legacyT(key);
  } catch {
    // Non-Nuxt context (unit tests / plain function calls): fall back to undefined
  }
  return undefined;
}
