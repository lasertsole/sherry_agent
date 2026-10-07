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
import { getCurrentInstance, reactive } from 'vue';
import enMessages from '@/i18n/locales/en.json';
import jaMessages from '@/i18n/locales/ja.json';
import koMessages from '@/i18n/locales/ko.json';
import zhMessages from '@/i18n/locales/zh.json';

/** The central dictionaries, keyed by locale (a test switches `locale.value`). */
const CENTRAL_MESSAGES: Record<string, Dict> = { en: enMessages, ja: jaMessages, ko: koMessages, zh: zhMessages };

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
 * The locale the suites run in; a test switches it to exercise other languages.
 * Reactive: components that watch `locale.value` (e.g. the preset viewer, which
 * reloads a language template when the locale changes) must see the switch.
 */
const currentLocale = reactive({ value: 'zh' });

/**
 * Local scope overlay: `<i18n lang="json">` block messages attached to the
 * mounted component by the `vitest-sfc-i18n-blocks` plugin (vitest.config.ts),
 * mirroring vue-i18n's component-local scope. Empty for components without
 * blocks and for calls outside a component instance (composables).
 *
 * The CURRENT locale's block wins (a test may switch `locale.value` to drive
 * locale-dependent rendering), falling back to `zh`.
 * @param blocks The component's `__i18n` blocks (captured at setup — a handler
 *   call has no current instance, so they cannot be re-read per call).
 */
function localOverlay(blocks: unknown[] | undefined): Dict {
  const overlay: Dict = {};
  for (const entry of blocks ?? []) {
    if (!entry || typeof entry !== 'object') continue;
    const dict = entry as Dict;
    const localized = dict[currentLocale.value] ?? dict.zh;
    if (localized && typeof localized === 'object') deepMerge(overlay, localized as Dict);
  }
  return overlay;
}

export function useI18n() {
  // The blocks are captured once (the instance is gone inside event handlers),
  // but the LOCALE is read per call: real vue-i18n re-renders on a locale switch,
  // and a block-resident key must follow it — resolving the overlay once at setup
  // left every inline-block label frozen in the setup-time language.
  const blocks = (getCurrentInstance()?.type as I18nBlockHost | undefined)?.__i18n;
  return {
    t: (key: string, params?: Record<string, unknown>) => {
      // Central keys resolve in the CURRENT locale (falling back to zh), so a
      // locale-switching test observes the same text the app would render.
      const central = CENTRAL_MESSAGES[currentLocale.value] ?? zhMessages;
      const overlay = localOverlay(blocks);
      let text = lookupTree(overlay, key) ?? lookupTree(central, key) ?? lookupTree(zhMessages, key) ?? key;
      if (params) {
        for (const [name, value] of Object.entries(params)) {
          text = text.replaceAll(`{${name}}`, String(value));
        }
      }
      return text;
    },
    locale: currentLocale,
    te: () => true,
    // Mirrors nuxt-i18n's composer entry: switching loads the pack (here the
    // reactive `currentLocale` the lookup above reads), so a suite can drive the
    // language picker and observe the switch.
    setLocale: async (code: string) => {
      currentLocale.value = code;
    }
  };
}

export const createI18n = () => ({ global: { t: (key: string) => lookupTree(zhMessages, key) ?? key } });
export const locale = currentLocale;
