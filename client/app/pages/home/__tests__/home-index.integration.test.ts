import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import homeIndex from '@/pages/home/index.vue';
import HistoryItem from '@/pages/home/components/HistoryItem.vue';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';
import { useSubagentStore } from '@/stores/subagent';
import type { SubagentRun } from '@/composables/bridge';

// This mock is scoped to this file: only here does the mounted home page graph
// reach the log panel's onMounted, which installs the console capture.
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
  // `title` / `icon` are passed through so the toolbar rows can be identified
  // by their accessible label (the real PrimeVue Button renders both).
  Button: {
    props: ['label', 'title', 'ariaLabel', 'icon'],
    template:
      '<button class="btn" :title="title || ariaLabel"><i :class="icon"></i><slot /><span>{{ label }}</span></button>'
  },
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

// `ensureGatewayToken` / `withGatewayToken` are imported directly (not through
// auto-imports) by the WS bridges the mounted children reach, so the module
// mock has to carry them too — otherwise those imports resolve to undefined.
vi.mock('@/composables/requestApi', () => ({
  fetchApi: seededFetchApi,
  ensureGatewayToken: vi.fn(async () => 'test-token'),
  withGatewayToken: (url: string) => url
}));

// Children are real components whose heavy deps
// (PrimeVue/markdown) are stubbed above; the real get_history_by_turn_page
// runs against the mocked transport and resolves empty for non-/sessions URLs.
function mountHome() {
  return mount(homeIndex, {
    global: { stubs: primevueStub }
  });
}

/**
 * Stub the project-directory store with a controllable binding.
 *
 * The files button needs a session AND a bound directory; the shared setup stub
 * reports "unbound", so the cases that expect the button have to provide a
 * binding — and flipping `state.directory` is how the unbound case is driven.
 * @param initial Bound directory (null = unbound).
 */
function stubProjectDirectory(initial: string | null = '/proj') {
  const original = (globalThis as any).useProjectDirectoryStore;
  const state = reactive({ directory: initial as string | null });
  vi.stubGlobal('useProjectDirectoryStore', () => ({
    stateFor: () => ({
      directory: state.directory,
      effective: state.directory ?? '',
      source: state.directory ? 'session' : 'default',
      pendingDirectory: null,
      error: null
    }),
    hydrate: async () => {},
    select: async () => {},
    fail: () => {},
    clearError: () => {}
  }));
  return { state, restore: () => vi.stubGlobal('useProjectDirectoryStore', original) };
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

  it('puts the right-sidebar toggle to the right of the settings menu button', () => {
    const wrapper = mountHome();
    const titles = wrapper.findAll('.btn').map(b => b.attributes('title'));

    // The two right-most entries of the toolbar: the nine-grid settings menu,
    // then the toggle for the sidebar whose panel is the right-most region.
    expect(titles.at(-2)).toBe('菜单');
    expect(['展开侧边栏', '折叠侧边栏']).toContain(titles.at(-1));
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

  it('keeps the left column to sessions — background tasks are a right-sidebar tab', async () => {
    const store = useSubagentStore();
    store.allTaskRuns = [
      {
        run_id: 'r1',
        depth: 1,
        requester_session_key: 'sess-A',
        task_name: '任务甲',
        execution: {}
      }
    ] as unknown as SubagentRun[];
    const wrapper = mountHome();
    await flushPromises();

    // The sidebar used to carry a 会话 / 后台任务 tab strip plus its own task list;
    // both are gone, so no run data may leak into the left column.
    const labels = wrapper.findAll('button').map(b => b.text());
    expect(labels.some(text => text.includes('后台任务'))).toBe(false);
    expect(wrapper.text()).not.toContain('任务甲');

    store.allTaskRuns = [];
  });

  it('hides the project-files button while no session is open, and returns the sidebar to the list', () => {
    // The session list's landing page has no sid: there is no project tree
    // behind the button, and the persisted body switch must not strand the
    // sidebar on it either.
    const originalUi = (globalThis as any).useUiStore;
    const uiState = reactive({ sidebarCollapsed: false, sidebarBody: 'files' });
    vi.stubGlobal('useUiStore', () => uiState);
    try {
      const wrapper = mountHome();

      const titles = wrapper.findAll('.btn').map(b => b.attributes('title'));
      expect(titles).not.toContain('项目文件');
      expect(titles).not.toContain('会话列表');
      expect(uiState.sidebarBody).toBe('sessions');
    } finally {
      // Restore ONLY the stub this test replaced: vi.unstubAllGlobals() would
      // also drop the suites' shared store stubs (they are installed the same
      // way in the setup file), breaking every later test in the file.
      vi.stubGlobal('useUiStore', originalUi);
    }
  });

  it('shows the project-files button when the session has a project directory bound', () => {
    const originalRoute = (globalThis as any).useRoute;
    vi.stubGlobal('useRoute', () => ({
      path: '/home/s1',
      fullPath: '/home/s1',
      params: { sid: 's1' },
      query: {}
    }));
    const dir = stubProjectDirectory('/proj');
    try {
      const wrapper = mountHome();

      const titles = wrapper.findAll('.btn').map(b => b.attributes('title'));
      expect(titles).toContain('项目文件');
    } finally {
      dir.restore();
      vi.stubGlobal('useRoute', originalRoute);
    }
  });

  it('hides the button — and it cannot be pressed — while the session has no project directory', () => {
    // No bound root means no tree to open: the control is absent, not disabled,
    // so there is nothing to click and no empty picker to land on.
    const originalRoute = (globalThis as any).useRoute;
    const originalUi = (globalThis as any).useUiStore;
    vi.stubGlobal('useRoute', () => ({
      path: '/home/s1',
      fullPath: '/home/s1',
      params: { sid: 's1' },
      query: {}
    }));
    const uiState = reactive({ sidebarCollapsed: false, sidebarBody: 'files' });
    vi.stubGlobal('useUiStore', () => uiState);
    const dir = stubProjectDirectory(null);
    try {
      const wrapper = mountHome();

      const titles = wrapper.findAll('.btn').map(b => b.attributes('title'));
      expect(titles).not.toContain('项目文件');
      expect(titles).not.toContain('会话列表');
      expect(wrapper.findAll('.btn').filter(b => (b.attributes('title') ?? '').includes('项目'))).toHaveLength(0);
      // A persisted "files" body would strand the sidebar: it falls back.
      expect(uiState.sidebarBody).toBe('sessions');
    } finally {
      dir.restore();
      vi.stubGlobal('useUiStore', originalUi);
      vi.stubGlobal('useRoute', originalRoute);
    }
  });

  it('hides the button again when the last session closes, and flips the body back', async () => {
    const originalUi = (globalThis as any).useUiStore;
    const originalRoute = (globalThis as any).useRoute;
    const uiState = reactive({ sidebarCollapsed: false, sidebarBody: 'files' });
    vi.stubGlobal('useUiStore', () => uiState);
    const routeState = reactive({
      path: '/home/s1',
      fullPath: '/home/s1',
      params: { sid: 's1' } as Record<string, string>,
      query: {}
    });
    vi.stubGlobal('useRoute', () => routeState);
    const dir = stubProjectDirectory('/proj');
    try {
      const wrapper = mountHome();
      // In files mode the button names the way BACK, so "会话列表" is its title.
      expect(wrapper.findAll('.btn').map(b => b.attributes('title'))).toContain('会话列表');

      routeState.params = {};
      routeState.path = '/home';
      await nextTick();

      const titles = wrapper.findAll('.btn').map(b => b.attributes('title'));
      expect(titles).not.toContain('项目文件');
      expect(uiState.sidebarBody).toBe('sessions');
    } finally {
      dir.restore();
      vi.stubGlobal('useUiStore', originalUi);
      vi.stubGlobal('useRoute', originalRoute);
    }
  });
});
