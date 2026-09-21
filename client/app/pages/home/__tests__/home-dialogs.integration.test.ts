import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive, ref } from 'vue';
import homeIndex from '@/pages/home/index.vue';

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

// Same worker-starvation guard as home-index.integration.test.ts: mounting the
// shell reaches LogsDialog's module graph, whose clientLog capture self-feeds
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

// The dialogs are the heavy leaves (PrimeVue + @antv/g2 + cropperjs). Replace each
// lazy dialog module with an inert stub so this suite exercises the shell's registry
// wiring, not the dialog internals: a rendered stub proves the dialog was opened.
const { dialogStub } = vi.hoisted(() => ({
  dialogStub: (tag: string) => ({
    name: `stub-${tag}`,
    props: ['modelValue'],
    emits: ['update:modelValue', 'changed', 'saved'],
    template: `<div data-test="${tag}" :data-open="String(modelValue)"><slot /></div>`
  })
}));

vi.mock('@/pages/home/components/SkillsDialog.vue', () => ({ __esModule: true, default: dialogStub('skills-dialog') }));
vi.mock('@/pages/home/components/StatsDialog.vue', () => ({ __esModule: true, default: dialogStub('stats-dialog') }));
vi.mock('@/pages/home/components/ConfigDialog.vue', () => ({ __esModule: true, default: dialogStub('config-dialog') }));
vi.mock('@/pages/home/components/PersonaDialog.vue', () => ({
  __esModule: true,
  default: dialogStub('persona-dialog')
}));
vi.mock('@/pages/home/components/MemoryDialog.vue', () => ({ __esModule: true, default: dialogStub('memory-dialog') }));
vi.mock('@/pages/home/components/HeartbeatDialog.vue', () => ({
  __esModule: true,
  default: dialogStub('heartbeat-dialog')
}));
vi.mock('@/pages/home/components/CronDialog.vue', () => ({ __esModule: true, default: dialogStub('cron-dialog') }));
vi.mock('@/pages/home/components/LogsDialog.vue', () => ({ __esModule: true, default: dialogStub('logs-dialog') }));
vi.mock('@/pages/home/components/ExtendDialog.vue', () => ({ __esModule: true, default: dialogStub('extend-dialog') }));
vi.mock('@/pages/home/components/NotificationDialog.vue', () => ({
  __esModule: true,
  default: dialogStub('notification-dialog')
}));

const seededFetchApi = vi.hoisted(() =>
  vi.fn(async (opts?: { url?: string }) =>
    opts?.url === '/sessions' ? [{ session_id: 's1', last_time: '20260617104200', title: '第一次对话' }] : []
  )
);
vi.mock('@/composables/requestApi', () => ({ fetchApi: seededFetchApi }));

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

describe('home/index.vue dialog registry (integration, backend mocked)', () => {
  beforeEach(() => {
    seededFetchApi.mockClear();
    uiState.settingsMenuOpen = false;
    uiState.sidebarCollapsed = false;
  });

  it('opens the notification dialog from the top-bar bell command', async () => {
    const wrapper = mountHome();
    await flushPromises();

    expect(wrapper.find('[data-test="notification-dialog"]').attributes('data-open')).toBe('false');

    await clickIconButton(wrapper, 'pi pi-bell');

    expect(wrapper.find('[data-test="notification-dialog"]').attributes('data-open')).toBe('true');
  });

  it('opens the logs dialog from the top-bar history command', async () => {
    const wrapper = mountHome();
    await flushPromises();

    await clickIconButton(wrapper, 'pi pi-history');

    expect(wrapper.find('[data-test="logs-dialog"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="logs-dialog"]').attributes('data-open')).toBe('true');
  });

  it('opens the dialog registered for a nine-grid tool', async () => {
    const wrapper = mountHome();
    await flushPromises();

    await clickIconButton(wrapper, 'pi pi-bars');
    const skillsEntry = wrapper.findAll('.dlg button').find(b => b.find('i.pi-bolt').exists());
    expect(skillsEntry, 'nine-grid skills entry').toBeTruthy();

    await skillsEntry!.trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="skills-dialog"]').attributes('data-open')).toBe('true');
  });

  it('closes a dialog through its own update:modelValue (v-model close path)', async () => {
    const wrapper = mountHome();
    await flushPromises();

    await clickIconButton(wrapper, 'pi pi-bell');
    const dialog = wrapper.findComponent({ name: 'stub-notification-dialog' });
    dialog.vm.$emit('update:modelValue', false);
    await flushPromises();

    expect(wrapper.find('[data-test="notification-dialog"]').attributes('data-open')).toBe('false');
  });

  it('keeps every other dialog closed when one opens', async () => {
    const wrapper = mountHome();
    await flushPromises();

    await clickIconButton(wrapper, 'pi pi-bell');

    for (const tag of ['skills', 'stats', 'config', 'persona', 'memory', 'heartbeat', 'cron', 'logs', 'extend']) {
      expect(wrapper.find(`[data-test="${tag}-dialog"]`).exists(), `${tag} dialog`).toBe(false);
    }
  });
});
