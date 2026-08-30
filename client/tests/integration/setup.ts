/**
 * Vitest setup for browser-mode integration tests.
 *
 * In a bare happy-dom environment neither the Nuxt SPA runtime NOR Nuxt's
 * auto-import system is booted. That leaves two gaps to fill on `globalThis`:
 *
 *  1. Nuxt auto-imports (useFetch, fetchApi, get_history_by_turn_page, useColorMode)
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

// Nuxt auto-import used by `requestApi.ts` (called from `get_history_by_turn_page`).
(globalThis as any).useFetch = vi.fn().mockReturnValue({
  data: { value: null },
  error: { value: null },
});

// Nuxt auto-import used by `messages.ts` / `workspace.ts` / `bridge.ts`.
(globalThis as any).fetchApi = vi.fn(() =>
  Promise.resolve({ code: 200, data: null }),
);

// Nuxt auto-import called inline at setup by `home/index.vue`
// (`get_history_by_turn_page('default', 0, 10, 1)`). Resolve to an empty list so the
// page mounts without touching the backend.
(globalThis as any).get_history_by_turn_page = vi.fn(async () => []);

// Nuxt auto-import used by `ModeSwitch.vue` for dark/light theme.
(globalThis as any).useColorMode = vi.fn(() => ({
  preference: 'light',
  value: 'light',
}));

// Nuxt auto-import used by `home/index.vue` / `ModeSwitch.vue` (Pinia UI store,
// stores/ui.ts). Faithful to the real store: `setTheme` is the unified theme
// write entry and forwards to the useColorMode singleton (resolved lazily so
// suites overriding useColorMode keep working); `toggleSidebar` flips local state.
// Suites needing to observe setTheme calls should stubGlobal their own instance
// (see mode-switch.integration.test.ts).
(globalThis as any).useUiStore = vi.fn(() => {
  const state = Vue.reactive({
    sidebarCollapsed: false,
    settingsMenuOpen: false,
    setTheme: (value: string) => {
      const colorMode = (globalThis as any).useColorMode?.();
      if (colorMode) colorMode.preference = value;
    },
    toggleSidebar: () => {
      state.sidebarCollapsed = !state.sidebarCollapsed;
    },
  });
  return state;
});

// Nuxt auto-import used by `ChatBox.vue` / `home/index.vue` for image preview.
// Mock the composable (module-scope `ref` in useImagePreview.ts would otherwise
// run before the vueAutoImports loop above, so we don't import the real one).
(globalThis as any).useImagePreview = vi.fn(() => ({
  previewSrc: Vue.ref(''),
  isPreviewVisible: Vue.ref(false),
  openPreview: vi.fn(),
  closePreview: vi.fn(),
}));

// Nuxt auto-import used by `home/index.vue` / `ConfigDialog.vue` for the global
// chat background image (composables/useChatBackground.ts, Dexie-persisted
// module singleton). Mocked for the same reason as useImagePreview: the real
// module holds module-scope refs and pulls in db.ts (Dexie/IndexedDB). The
// shape is faithful to the real composable: empty background plus computed
// styles derived from the useColorMode singleton above.
(globalThis as any).useChatBackground = vi.fn(() => {
  const backgroundUrl = Vue.ref('');
  const backgroundOpacity = Vue.ref(0);
  const backgroundLoaded = Vue.ref(false);
  return {
    backgroundUrl,
    backgroundOpacity,
    backgroundLoaded,
    loadBackground: vi.fn(async () => {
      backgroundLoaded.value = true;
    }),
    setBackground: vi.fn(async () => {}),
    setBackgroundOpacity: vi.fn(async () => {}),
    chatBackgroundStyle: Vue.computed(() => {
      if (!backgroundUrl.value) return undefined;
      return {
        backgroundImage: `url("${backgroundUrl.value}")`,
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        backgroundRepeat: 'no-repeat',
      };
    }),
    chatBackgroundOverlayStyle: Vue.computed(() => {
      const colorMode = (globalThis as any).useColorMode?.() ?? { value: 'light' };
      return {
        backgroundColor: colorMode.value === 'light' ? '#ffffff' : '#000000',
        opacity: backgroundOpacity.value / 100,
      };
    }),
  };
});

// Nuxt auto-imports consumed at setup top-level by `home/index.vue` (:246-247)
// and `SessionSidebar.vue` (:290-292). No vue-router is installed in bare
// Vitest, so provide inert navigation + a static route shape.
(globalThis as any).useRouter = vi.fn(() => ({
  push: vi.fn(async () => {}),
  replace: vi.fn(async () => {}),
  back: vi.fn(),
  go: vi.fn(),
  currentRoute: Vue.ref({ path: '/home', params: {}, query: {} }),
}));
(globalThis as any).useRoute = vi.fn(() => ({
  path: '/home',
  fullPath: '/home',
  params: {} as Record<string, string>,
  query: {} as Record<string, string>,
}));
(globalThis as any).useLocalePath = vi.fn((to?: unknown) =>
  typeof to === 'string' ? to : '/',
);

// Pinia auto-import used by `home/index.vue` (:307, :310) to destructure the
// UI store. Faithful to Pinia's contract: state keys become refs bound to the
// reactive store, action functions are excluded.
(globalThis as any).storeToRefs = (store: Record<string, unknown>) => {
  const refs: Record<string, unknown> = {};
  for (const key of Object.keys(store)) {
    if (typeof store[key] !== 'function') refs[key] = Vue.toRef(store, key);
  }
  return refs;
};

// happy-dom has no reachable backend, and ws.ts (useWs / useSubagentWs) opens
// real WebSocket singletons whose onclose schedules an unbounded 5s reconnect
// loop. That pins the worker event loop until vitest force-kills the fork
// ("Worker exited unexpectedly" + minutes-long run). Replace WebSocket with an
// inert fake that reports OPEN immediately: ws.ts's singleton-reuse branch then
// short-circuits, onclose never fires, and no reconnect timers remain.
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  url: string;
  readyState = FakeWebSocket.OPEN;
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  constructor(url: string) {
    this.url = url;
    // Microtask so handlers assigned synchronously after `new WebSocket(...)`
    // still fire, and the first useWs() call completes its connect() cleanly.
    queueMicrotask(() => this.onopen?.({ type: 'open' }));
  }
  send() {}
  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() {
    return false;
  }
}
(globalThis as any).WebSocket = FakeWebSocket;

// clientLog.ts's console.* capture (installed by LogsDialog's onMounted in the
// home page graph) self-feeds in happy-dom: pushEntry -> Dexie add() rejects
// (no IndexedDB) -> the persistence-failure handler calls console.warn -> the
// capture re-captures that -> pushEntry again... This infinite microtask loop
// starves flushPromises (tests never complete, "tests 0ms") and OOMs the
// worker at the 4GB heap limit (~5M lines of "[clientLog] failed to persist
// entry" spam). Keep the module's real API but never install the capture in
// integration tests.
vi.mock('@/composables/clientLog', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/composables/clientLog')>();
  return {
    ...actual,
    installClientLogCapture: () => {},
  };
});
