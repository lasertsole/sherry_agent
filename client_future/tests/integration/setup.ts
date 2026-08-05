/**
 * Vitest setup for browser-mode integration tests.
 *
 * In a bare happy-dom environment neither the Nuxt SPA runtime NOR Nuxt's
 * auto-import system is booted. That leaves two gaps to fill on `globalThis`:
 *
 *  1. Nuxt auto-imports (useFetch, fetchApi, get_history_by_page, useColorMode)
 *     → controllably stubbed so components never reach the backend.
 *  2. Vue Composition API aliases (ref, computed, watch, ...) which the `.vue`
 *     SFCs use WITHOUT an explicit `import`. In Nuxt these come from Nuxt's
 *     unimport; in bare Vitest we re-export the real implementations from `vue`
 *     so the components exercise their genuine reactivity logic.
 */
import { vi } from 'vitest';
import * as Vue from 'vue';

// Vue auto-imports consumed by the SFC sources under test (Nuxt provides these
// transparently; bare Vitest does not). Re-export the real implementations.
const vueAutoImports = {
  ref: Vue.ref,
  computed: Vue.computed,
  watch: Vue.watch,
  watchEffect: Vue.watchEffect,
  watchPostEffect: Vue.watchPostEffect,
  onMounted: Vue.onMounted,
  onBeforeUnmount: Vue.onBeforeUnmount,
  readonly: Vue.readonly,
  shallowRef: Vue.shallowRef,
  isRef: Vue.isRef,
  toRef: Vue.toRef,
  toRefs: Vue.toRefs,
  reactive: Vue.reactive,
  nextTick: Vue.nextTick,
  useTemplateRef: Vue.useTemplateRef,
  defineModel: () => Vue.ref(''), // defineModel is a compiler macro; fallback no-op
};
for (const [name, impl] of Object.entries(vueAutoImports)) {
  (globalThis as any)[name] = impl;
}

// Nuxt auto-import used by `requestApi.ts` (called from `get_history_by_page`).
(globalThis as any).useFetch = vi.fn().mockReturnValue({
  data: { value: null },
  error: { value: null },
});

// Nuxt auto-import used by `messages.ts` / `workspace.ts` / `bridge.ts`.
(globalThis as any).fetchApi = vi.fn(() =>
  Promise.resolve({ code: 200, data: null }),
);

// Nuxt auto-import called inline at setup by `home/index.vue`
// (`get_history_by_page('main', 10, 10, 1)`). Resolve to an empty list so the
// page mounts without touching the backend.
(globalThis as any).get_history_by_page = vi.fn(async () => []);

// Nuxt auto-import used by `ModeSwitch.vue` for dark/light theme.
(globalThis as any).useColorMode = vi.fn(() => ({
  preference: 'light',
  value: 'light',
}));
