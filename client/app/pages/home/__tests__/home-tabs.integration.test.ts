import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive, ref } from 'vue';
import homeIndex from '@/pages/home/index.vue';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';

// The shared setup shim exposes the UI store as a plain reactive object, but
// Pinia's real storeToRefs only surfaces ref/reactive entries — so
// `settingsMenuOpen` (a primitive) would read as undefined and the nine-grid
// toggle could not be exercised. Mirror the real setup-store shape (refs +
// actions) for this suite.
const uiState = reactive({
  sidebarCollapsed: ref(false),
  settingsMenuOpen: ref(false),
  todoDockCollapsed: ref(false),
  setTheme: (value: string) => {
    const colorMode = (globalThis as { useColorMode?: () => { preference: string } }).useColorMode?.();
    if (colorMode) colorMode.preference = value;
  },
  toggleSidebar: () => {
    uiState.sidebarCollapsed = !uiState.sidebarCollapsed;
  },
  toggleTodoDock: () => {
    uiState.todoDockCollapsed = !uiState.todoDockCollapsed;
  }
});
vi.stubGlobal('useUiStore', () => uiState);

// Right sidebar (log viewer / statistics tabs): real store shape, spy actions so
// the command wiring can be asserted without rendering the panels.
const rightSidebarState = reactive({
  collapsed: ref(true),
  tabs: ref<Array<{ id: string; kind: string; scope: string }>>([]),
  activeTabId: ref<string | null>(null),
  activeScope: ref('global'),
  width: ref(420),
  setWidth: () => {},
  fitToViewport: () => {},
  toggle: () => {
    rightSidebarState.collapsed = !rightSidebarState.collapsed;
  },
  expand: () => {
    rightSidebarState.collapsed = false;
  },
  openTab: vi.fn((kind: string) => `tab-${kind}`),
  tabsInScope: () => [],
  setActiveScope: () => {},
  activateTab: () => {},
  closeTab: () => {}
});
vi.stubGlobal('useRightSidebarStore', () => rightSidebarState);

// Same worker-starvation guard as home-index.integration.test.ts: mounting the
// shell reaches the log panel's module graph, whose clientLog capture self-feeds
// under happy-dom (no IndexedDB). Keep the real module but never install it.
vi.mock('@/composables/clientLog', async importOriginal => {
  const actual = await importOriginal<typeof import('@/composables/clientLog')>();
  return {
    ...actual,
    installClientLogCapture: () => {}
  };
});

// Dexie has no IndexedDB in happy-dom; neutralize persistence so the fetchApi seed
// stays the only data source (mirrors home-index.integration.test.ts).
vi.mock('@/composables/db', async importOriginal => {
  const actual = await importOriginal<typeof import('@/composables/db')>();
  return {
    ...actual,
    readCachedMessages: async () => [],
    cachedMaxTurnNum: async () => 0,
    cacheMessages: async () => {},
    clearCachedSession: async () => {},
    readCachedCharacter: async () => undefined,
    cacheCharacter: async () => {},
    clearCachedCharacter: async () => {},
    readCachedSessionMetaList: async () => [],
    cacheSessionMeta: async () => {},
    clearCachedSessionMeta: async () => {},
    saveSessionTitleOverride: async () => {},
    readSessionTitleOverrides: async () => new Map<string, string>(),
    clearSessionTitleOverride: async () => {},
    saveDraftTurn: async () => {},
    readDraftTurns: async () => [],
    clearDraftTurn: async () => {},
    clearDraftSession: async () => {}
  };
});

const seededFetchApi = vi.hoisted(() =>
  vi.fn(async (opts?: { url?: string }) =>
    opts?.url === '/sessions' ? [{ session_id: 's1', last_time: '20260617104200', title: '第一次对话' }] : []
  )
);
// The mounted dialogs reach the WS bridges, which import these two directly,
// so the module mock must carry them (see home-index.integration.test.ts).
vi.mock('@/composables/requestApi', () => ({
  fetchApi: seededFetchApi,
  ensureGatewayToken: vi.fn(async () => 'test-token'),
  withGatewayToken: (url: string) => url
}));

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>'
  },
  Button: {
    name: 'Button',
    props: ['label', 'icon', 'title', 'ariaLabel', 'variant'],
    emits: ['click'],
    template: '<button class="btn" :title="title" @click="$emit(\'click\')"><slot />{{ label }}</button>'
  },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' },
  Dialog: {
    name: 'Dialog',
    props: ['visible'],
    template: '<div class="dlg" v-if="visible"><slot /></div>'
  }
};

function mountHome() {
  return mount(homeIndex, { global: { stubs: primevueStub } });
}

/**
 * Click the top-bar button carrying the given PrimeIcons class.
 * @param wrapper
 * @param icon
 */
async function clickIconButton(wrapper: ReturnType<typeof mountHome>, icon: string) {
  const button = wrapper.findAllComponents({ name: 'Button' }).find(b => b.props('icon') === icon);
  expect(button, `top-bar button ${icon}`).toBeTruthy();
  await button!.trigger('click');
  await flushPromises();
}

describe('home/index.vue toolbar registry (integration, backend mocked)', () => {
  beforeEach(() => {
    seededFetchApi.mockClear();
    rightSidebarState.openTab.mockClear();
    uiState.settingsMenuOpen = false;
    uiState.sidebarCollapsed = false;
  });

  it('opens the notification TAB from the top-bar bell command', async () => {
    const wrapper = mountHome();
    await flushPromises();

    await clickIconButton(wrapper, 'pi pi-bell');

    // The bell routes into the right sidebar's strip (never a dialog).
    expect(rightSidebarState.openTab).toHaveBeenCalledWith('notification');
  });

  it('keeps the theme switch and the language picker in the sidebar title row', async () => {
    const wrapper = mountHome();
    await flushPromises();

    // Both controls are workspace settings: they live in the left sidebar's title
    // row (right edge), not in the top toolbar, so the toolbar's container query
    // no longer has to hide anything for them.
    const titleRow = wrapper.get('[data-test="sidebar-title-row"]');
    expect(titleRow.findComponent(ModeSwitch).exists()).toBe(true);
    expect(titleRow.find('[data-test="sidebar-locale"]').exists()).toBe(true);
    // The toolbar keeps no copy of either (the old narrow-column guards are gone).
    expect(wrapper.findAll('.\\@max-\\[620px\\]\\:hidden\\!').length).toBe(0);
  });

  it('keeps the log viewer out of the top bar (it is a nine-grid entry)', async () => {
    const wrapper = mountHome();
    await flushPromises();

    const topBarIcons = wrapper.findAllComponents({ name: 'Button' }).map(b => b.props('icon'));
    expect(topBarIcons).not.toContain('pi pi-history');
  });

  it('opens a right-sidebar tab for every nine-grid entry', async () => {
    // Every settings-menu tile, in registry order: icon → panel kind.
    const entries: ReadonlyArray<[string, string]> = [
      ['pi pi-bolt', 'skills'],
      ['pi pi-sitemap', 'knowledgeGraph'],
      ['pi pi-chart-bar', 'stats'],
      ['pi pi-history', 'logs'],
      ['pi pi-sliders-h', 'systemConfig'],
      ['pi pi-user', 'persona'],
      ['pi pi-database', 'memory'],
      ['pi pi-heart', 'heartbeat'],
      ['pi pi-clock', 'cron'],
      ['pi pi-id-card', 'account'],
      ['puzzle-icon', 'extend']
    ];
    const wrapper = mountHome();
    await flushPromises();

    for (const [icon, kind] of entries) {
      rightSidebarState.openTab.mockClear();

      await clickIconButton(wrapper, 'pi pi-bars');
      const entry = wrapper.findAll('.dlg button').find(b => b.find(`i.${icon.split(' ').join('.')}`).exists());
      expect(entry, `nine-grid entry ${icon}`).toBeTruthy();
      await entry!.trigger('click');
      await flushPromises();

      expect(rightSidebarState.openTab).toHaveBeenCalledWith(kind);
    }
  });
});
