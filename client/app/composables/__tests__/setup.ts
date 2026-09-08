/**
 * Vitest setup for composables tests.
 *
 * Stubs Nuxt auto-imports / runtime globals that are not available in a bare
 * happy-dom environment, so the production composables can be exercised
 * directly without mounting the full Nuxt SPA.
 */
import { vi } from 'vitest';
import * as Vue from 'vue';
import { config } from '@vue/test-utils';
import { vDebounce } from '~/directives/debounce';
import { vSafeHtml } from '~/directives/safeHtml';

// The Nuxt plugin `app/plugins/directives.ts` registers v-debounce / v-safe-html
// on the app instance; bare Vitest never boots Nuxt plugins, so mirror that
// global registration here for every mount.
config.global.directives = {
  debounce: vDebounce,
  'safe-html': vSafeHtml
};

// `requestApi.ts` calls ofetch's global `$fetch(request, opts)` and expects it
// to resolve with the parsed response body (rejecting only on unrecoverable
// errors, which requestApi catches internally). Tests can override this
// global per-file via `vi.stubGlobal('$fetch', mock)`.
(globalThis as any).$fetch = vi.fn().mockResolvedValue(null);

// Composables under `app/composables/` (messages, workspace, bridge, ...)
// call `fetchApi` as a *Nuxt auto-import* (there is no explicit import in the
// source files), so it is NOT available in a bare Vitest environment. Tests
// stub this global per-file via `vi.stubGlobal('fetchApi', mock)`. Without it
// `import { fetchApi } from '../requestApi'` would fail for the explicit
// importer (requestApi.test.ts) — so keep that explicit import path working
// by leaving `import ... from '../requestApi'` inside test files untouched.
(globalThis as any).fetchApi = vi.fn(() => Promise.resolve({ code: 200, data: null }));

// ---------------------------------------------------------------------------
// SFC integration-suite stubs (real .vue components mounted via @vue/test-utils)
// ---------------------------------------------------------------------------

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
  defineModel: () => Vue.ref('') // defineModel is a compiler macro; fallback no-op
};
for (const [name, impl] of Object.entries(vueAutoImports)) {
  (globalThis as any)[name] = impl;
}

// Nuxt auto-import used by `requestApi.ts` (called from `get_history_by_turn_page`).
(globalThis as any).useFetch = vi.fn().mockReturnValue({
  data: { value: null },
  error: { value: null }
});

// Nuxt auto-import called inline at setup by `home/index.vue`
// (`get_history_by_turn_page('default', 0, 10, 1)`). Resolve to an empty list so the
// page mounts without touching the backend.
(globalThis as any).get_history_by_turn_page = vi.fn(async () => []);

// Nuxt auto-import used by `ModeSwitch.vue` for dark/light theme.
(globalThis as any).useColorMode = vi.fn(() => ({
  preference: 'light',
  value: 'light'
}));

// PrimeVue ConfirmationService auto-import (`useConfirm`) consumed by
// HistoryItem.vue / SessionSidebar.vue at setup top-level. Provide the
// documented contract shape ({ require }) so components mount without the
// PrimeVue plugin graph.
(globalThis as any).useConfirm = vi.fn(() => ({ require: vi.fn() }));

// Nuxt auto-import used by `home/index.vue` / `ModeSwitch.vue` (Pinia UI store,
// stores/ui.ts). Faithful to the real store: `setTheme` is the unified theme
// write entry and forwards to the useColorMode singleton (resolved lazily so
// suites overriding useColorMode keep working); `toggleSidebar` flips local state.
// Suites needing to observe setTheme calls should stubGlobal their own instance
// (see mode-switch tests).
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
    }
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
  closePreview: vi.fn()
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
        backgroundRepeat: 'no-repeat'
      };
    }),
    chatBackgroundOverlayStyle: Vue.computed(() => {
      const colorMode = (globalThis as any).useColorMode?.() ?? { value: 'light' };
      return {
        backgroundColor: colorMode.value === 'light' ? '#ffffff' : '#000000',
        opacity: backgroundOpacity.value / 100
      };
    })
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
  currentRoute: Vue.ref({ path: '/home', params: {}, query: {} })
}));
(globalThis as any).useRoute = vi.fn(() => ({
  path: '/home',
  fullPath: '/home',
  params: {} as Record<string, string>,
  query: {} as Record<string, string>
}));
(globalThis as any).useLocalePath = vi.fn((to?: unknown) => (typeof to === 'string' ? to : '/'));

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
