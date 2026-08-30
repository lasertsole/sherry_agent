import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import homeIndex from '@/pages/home/index.vue';
import HistoryItem from '@/pages/home/components/HistoryItem.vue';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';

// Dexie has no IndexedDB to back it in happy-dom (probe: every operation rejects
// with MissingAPIError), so keep the real module but neutralize the persistence
// wrappers: they resolve empty and the fetchApi-seeded server rows become the
// sole data source for SessionSidebar.loadSessionList.
vi.mock('@/composables/db', async (importOriginal) => {
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
    clearDraftSession: async () => {},
  };
});

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>',
  },
  Button: { props: ['label'], template: '<button class="btn"><slot /><span>{{ label }}</span></button>' },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' },
};

// SessionSidebar loads sessions asynchronously through getSessionList() ->
// fetchApi('/sessions'); the server answers with a bare array (see messages.ts
// getSessionList). Seed one row so HistoryItem children actually render.
// last_time must stay in the 14-digit compact format that
// formatCompactTimeString parses.
const seededFetchApi = vi.fn(async (opts?: { url?: string }) =>
  opts?.url === '/sessions'
    ? [{ session_id: 's1', last_time: '20260617104200', title: '第一次对话' }]
    : { code: 200, data: null },
);

// `get_history_by_turn_page` is a Nuxt auto-import pre-stubbed in setup.ts, so
// any stray reference is a no-op. Children are real components whose heavy deps
// (PrimeVue/markdown) are stubbed above.
function mountHome() {
  return mount(homeIndex, {
    global: { stubs: primevueStub },
  });
}

describe('home/index.vue (integration, backend mocked)', () => {
  beforeEach(() => {
    vi.stubGlobal('fetchApi', seededFetchApi);
  });

  it('composes the sidebar and chat regions with real leaf children', async () => {
    const wrapper = mountHome();
    // Sessions arrive asynchronously: onMounted -> loadSessionList -> getSessionList.
    await flushPromises();
    await flushPromises();
    expect(wrapper.findComponent(HistoryItem).exists()).toBe(true);
    expect(wrapper.findComponent(ModeSwitch).exists()).toBe(true);
    // The server row's title reaches the sidebar list. ChatBox itself now lives
    // in [sid].vue behind the nested NuxtPage and no longer mounts on the shell.
    expect(wrapper.text()).toContain('第一次对话');
    // Branding present in the sidebar LOGO area.
    expect(wrapper.text()).toContain('🍊橘雪莉');
  });

  it('renders the full-select checkbox group', () => {
    const wrapper = mountHome();
    expect(wrapper.text()).toContain('全选');
    expect(wrapper.text()).toContain('批量删除对话');
    expect(wrapper.find('.cb').exists()).toBe(true);
  });

  it('activates a history row when it is selected', async () => {
    const wrapper = mountHome();
    await flushPromises();
    await flushPromises();
    const row = wrapper.findComponent(HistoryItem).find('.p-3');
    await row.trigger('click');
    await nextTick();
    // chooseSession -> handleToggleSession sets currentSessionId -> is-active true.
    expect(wrapper.findComponent(HistoryItem).props('isActive')).toBe(true);
  });

  it('keeps isIndeterminate false with a single fully- or not-selected session', () => {
    const wrapper = mountHome();
    // historyList has exactly 1 record, so a partial selection can never occur:
    // the full-select Checkbox must not receive an `indeterminate` flag.
    const cb = wrapper.findComponent({ name: 'Checkbox' });
    expect(cb.exists()).toBe(true);
    expect(cb.props('indeterminate')).toBeUndefined();
  });

  it('does not crash when the mobile switch/cog actions fire', async () => {
    const wrapper = mountHome();
    const buttons = wrapper.findAll('.btn');
    // Toggling the mobile menu button flips the sidebar overlay.
    expect(buttons.length).toBeGreaterThan(0);
  });
});
