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
 * (e.g. 橘雪莉 / 我). A tiny `{name}` interpolator covers parameterized keys
 * (e.g. `chatBox.modelMeta`).
 */
import zhMessages from '@/i18n/locales/zh.json';

/** 逐段下钻 zh 文案树；未命中的 key 原样返回（等价于 i18n 的 missing 警告行为） */
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
