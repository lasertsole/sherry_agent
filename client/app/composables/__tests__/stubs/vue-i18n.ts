/**
 * Vitest-only stub for `vue-i18n` (unit + integration suites).
 *
 * SFCs and composables import `{ useI18n } from 'vue-i18n'` explicitly. In this
 * pnpm workspace `vue-i18n` is nested inside the Nuxt i18n plugin and is not
 * top-level resolvable, so Vite's transform step fails before Vitest's
 * `vi.mock` can intercept the specifier.
 *
 * Keys resolve against the real `zh` locale messages, with a component-local
 * overlay on top: the `vitest-sfc-i18n-blocks` plugin (vitest.config.ts)
 * attaches SFC `<i18n lang="json">` block messages to the compiled component
 * (`__i18n`), mirroring vue-i18n's local scope with global fallback. Unknown
 * keys fall back to the key itself (missing-key behavior). A tiny `{name}`
 * interpolator covers parameterized keys (e.g. `chatBox.modelMeta`).
 */
import { getCurrentInstance } from 'vue';
import zhMessages from '@/i18n/locales/zh.json';

type Dict = { [key: string]: unknown };

interface I18nBlockHost {
  __i18n?: unknown[];
}

/**
 * Walks down a message tree segment by segment; undefined on a miss
 * @param dict
 * @param key
 */
function lookupTree(dict: Dict, key: string): string | undefined {
  const value = key
    .split('.')
    .reduce<unknown>(
      (node, part) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
      dict
    );
  return typeof value === 'string' ? value : undefined;
}

function deepMerge(target: Dict, source: Dict): void {
  for (const [key, value] of Object.entries(source)) {
    if (value !== null && typeof value === 'object') {
      if (target[key] === undefined || typeof target[key] !== 'object') target[key] = {};
      deepMerge(target[key] as Dict, value as Dict);
    } else {
      target[key] = value;
    }
  }
}

/**
 * Local scope overlay: `<i18n lang="json">` block messages attached to the
 * mounted component by the `vitest-sfc-i18n-blocks` plugin (vitest.config.ts),
 * mirroring vue-i18n's component-local scope. Empty for components without
 * blocks and for calls outside a component instance (composables).
 */
function localOverlay(): Dict {
  const blocks = (getCurrentInstance()?.type as I18nBlockHost | undefined)?.__i18n;
  const overlay: Dict = {};
  for (const entry of blocks ?? []) {
    if (!entry || typeof entry !== 'object') continue;
    const zh = (entry as Dict).zh;
    if (zh && typeof zh === 'object') deepMerge(overlay, zh as Dict);
  }
  return overlay;
}

export function useI18n() {
  const overlay = localOverlay();
  return {
    t: (key: string, params?: Record<string, unknown>) => {
      let text = lookupTree(overlay, key) ?? lookupTree(zhMessages, key) ?? key;
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

export const createI18n = () => ({ global: { t: (key: string) => lookupTree(zhMessages, key) ?? key } });
export const locale = { value: 'zh' };
