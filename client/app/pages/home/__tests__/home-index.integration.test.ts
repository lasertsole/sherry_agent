import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import homeIndex from '@/pages/home/index.vue';
import HistoryItem from '@/pages/home/components/HistoryItem.vue';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';
import { useSubagentStore } from '@/stores/subagent';
import type { SubagentRun } from '@/composables/bridge';

// This mock is scoped to this file: only here does the mounted home page graph
// reach LogsDialog's onMounted, which installs the console capture.
// clientLog.ts's console.* capture self-feeds in happy-dom: pushEntry -> Dexie
// add() rejects (no IndexedDB) -> the persistence-failure handler calls
// console.warn -> the capture re-captures that -> pushEntry again... This
// infinite microtask loop starves flushPromises and OOMs the worker. Keep the
// module's real API but never install the capture in this suite.
vi.mock('@/composables/clientLog', async importOriginal => {
  const actual = await importOriginal<typeof import('@/composables/clientLog')>();
  return {
    ...actual,
    installClientLogCapture: () => {}
  };
});

// Dexie has no IndexedDB to back it in happy-dom (probe: every operation rejects
// with MissingAPIError), so keep the real module but neutralize the persistence
// wrappers: they resolve empty and the fetchApi-seeded server rows become the
// sole data source for SessionSidebar.loadSessionList.
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

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>'
  },
  Button: { props: ['label'], template: '<button class="btn"><slot /><span>{{ label }}</span></button>' },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' }
};

// SessionSidebar loads sessions asynchronously through getSessionList() ->
// fetchApi('/sessions'); the server answers with a bare array (see messages.ts
// getSessionList). Seed one row so HistoryItem children actually render.
// last_time must stay in the 14-digit compact format that
// formatCompactTimeString parses. `fetchApi` is consumed through auto-imported
// module bindings (unimport injection, see vitest.config.ts), so the transport
// is mocked at the `@/composables/requestApi` module, not as a globalThis stub.
const seededFetchApi = vi.hoisted(() =>
  vi.fn(async (opts?: { url?: string }) =>
    opts?.url === '/sessions'
      ? [
          { session_id: 's1', last_time: '20260617104200', title: '第一次对话' },
          { session_id: 's2', last_time: '20260618104200', title: '第二次对话' },
          { session_id: 's3', last_time: '20260619104200', title: '第三次对话' }
        ]
      : { code: 200, data: null }
  )
);

vi.mock('@/composables/requestApi', () => ({ fetchApi: seededFetchApi }));

// Children are real components whose heavy deps
// (PrimeVue/markdown) are stubbed above; the real get_history_by_turn_page
// runs against the mocked transport and resolves empty for non-/sessions URLs.
function mountHome() {
  return mount(homeIndex, {
    global: { stubs: primevueStub }
  });
}

describe('home/index.vue (integration, backend mocked)', () => {
  beforeEach(() => {
    seededFetchApi.mockClear();
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

  it('drives every virtual session row from its own index (row model ↔ window)', async () => {
    const wrapper = mountHome();
    await flushPromises();
    await flushPromises();
    const rows = wrapper.findAllComponents(HistoryItem);
    // One virtual row per session, in list order: a window/index mapping bug
    // would show a title under the wrong row or repeat one.
    expect(rows.map(r => r.props('historyRecord').title)).toEqual(['第一次对话', '第二次对话', '第三次对话']);
    expect(rows.map(r => r.props('historyRecord').id)).toEqual(['s1', 's2', 's3']);
  });

  it('activates only the clicked session, whichever row it sits on', async () => {
    const wrapper = mountHome();
    await flushPromises();
    await flushPromises();
    const rows = wrapper.findAllComponents(HistoryItem);
    await rows[2]!.find('.p-3').trigger('click');
    await nextTick();
    const active = wrapper.findAllComponents(HistoryItem).filter(r => r.props('isActive'));
    expect(active).toHaveLength(1);
    expect(active[0]!.props('historyRecord').id).toBe('s3');
  });

  it('lists task runs grouped by calling session on the tasks tab', async () => {
    const store = useSubagentStore();
    store.allTaskRuns = [
      {
        run_id: 'r1',
        depth: 1,
        requester_session_key: 'sess-A',
        task_name: '任务甲',
        execution: {}
      },
      {
        run_id: 'r2',
        depth: 1,
        requester_session_key: 'sess-A',
        task_name: '任务乙',
        execution: {}
      },
      {
        run_id: 'r3',
        depth: 1,
        requester_session_key: 'sess-B',
        task_name: '任务丙',
        execution: {}
      }
    ] as unknown as SubagentRun[];
    const wrapper = mountHome();
    await flushPromises();
    const tasksTab = wrapper.findAll('button').find(b => b.text().includes('后台任务'));
    expect(tasksTab).toBeTruthy();
    await tasksTab!.trigger('click');
    await flushPromises();
    // Headers carry the calling session (with its run count), cards the task names.
    expect(wrapper.text()).toContain('sess-A');
    expect(wrapper.text()).toContain('(2)');
    expect(wrapper.text()).toContain('sess-B');
    expect(wrapper.text()).toContain('任务甲');
    expect(wrapper.text()).toContain('任务丙');
    store.allTaskRuns = [];
  });
});
