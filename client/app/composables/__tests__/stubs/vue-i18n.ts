/**
 * Vitest-only stub for `vue-i18n` (integration suite).
 *
 * SFCs under tests/integration import `{ useI18n } from 'vue-i18n'` explicitly.
 * In this pnpm workspace `vue-i18n` is nested inside the Nuxt i18n plugin and is
 * not top-level resolvable, so Vite's transform step fails before Vitest's
 * `vi.mock` can intercept the specifier (same root cause as the unit-suite stub
 * in `app/composables/__tests__/stubs/vue-i18n.ts`).
 *
 * Unlike the unit stub (identity `t`), this one resolves keys against the real
 * `zh` locale messages, because integration tests assert rendered Chinese copy
 * from the zh locale. A tiny `{name}` interpolator covers parameterized keys
 * (e.g. `chatBox.modelMeta`).
 */
import zhMessages from '@/i18n/locales/zh.json';

/** Walks down the zh message tree segment by segment; returns the key itself on a miss (mirroring i18n missing-key warning behavior) */
function lookup(key: string): string {
  const value = key
    .split('.')
    .reduce<unknown>(
      (node, part) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
      zhMessages
    );
  return typeof value === 'string' ? value : key;
}

export function useI18n() {
  return {
    t: (key: string, params?: Record<string, unknown>) => {
      let text = lookup(key);
      if (params) {
        for (const [name, value] of Object.entries(params)) {
          text = text.replaceAll(`{${name}}`, String(value));
        }
      }
      return text;
    },
    locale: { value: 'zh' },
    te: () => true
  };
}

export const createI18n = () => ({ global: { t: (key: string) => lookup(key) } });
export const locale = { value: 'zh' };
