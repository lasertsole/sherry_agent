/**
 * Vitest-only stub for `vue-i18n`.
 *
 * `app/composables/useSubagentTasks.ts` imports `{ useI18n } from 'vue-i18n'`
 * explicitly. In this pnpm workspace `vue-i18n@11.4.8` is not top-level
 * resolvable (it is nested inside the Nuxt i18n plugin), so Vite's transform
 * step fails before Vitest's `vi.mock` can intercept the specifier. This stub
 * is wired via `resolve.alias` in `vitest.config.ts` so the import resolves to
 * a real file that returns a deterministic `t` identity function.
 */
export function useI18n() {
  return {
    t: (key: string) => key,
    locale: { value: 'zh' },
    te: () => false
  };
}

export const createI18n = () => ({ global: { t: (key: string) => key } });
export const locale = { value: 'zh' };
