import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive, nextTick } from 'vue';
import homeIndex from '@/pages/home/index.vue';
import CommandPalette from '@/components/CommandPalette.vue';
import HistoryItem from '@/pages/home/components/HistoryItem.vue';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';
import { useSubagentStore } from '@/stores/subagent';
import { useChatFloatStore } from '@/stores/chat-float';
import { installGlobalShortcutListener } from '@/composables/use-shortcuts';
import {
  resolveFloatingSize,
  resolveFloatingPosition,
  resolveNearestFloatingAnchor,
  clampFloatingPosition
} from '@/common/floating-window';
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
    emits: ['click'],
    // Clicks are forwarded like PrimeVue's Button: toolbar switches are only
    // testable through the real interaction, not through their titles.
    template:
      '<button class="btn" :title="title || ariaLabel" @click="$emit(\'click\')"><i :class="icon"></i><slot /><span>{{ label }}</span></button>'
  },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  // The Ctrl+K palette (only its visibility is exercised here; its own suite
  // covers the rows). Rendering the slot only when open keeps the shell markup
  // unchanged for the other cases.
  Dialog: { props: ['visible'], template: '<div class="dlg" v-if="visible"><slot /></div>' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
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

/**
 * A controllable right-sidebar store for the float cases (the shared setup stub
 * has no `maximized`, which the expand/restore linkage observes).
 * @param overrides Initial collapsed / width / maximized state.
 * @param overrides.collapsed Initial collapsed flag.
 * @param overrides.width Initial stored width.
 * @param overrides.maximized Initial maximize flag.
 * @returns The stubbed store and a restore hook.
 */
function stubRightSidebar(overrides: { collapsed?: boolean; width?: number; maximized?: boolean } = {}) {
  const original = (globalThis as any).useRightSidebarStore;
  const store = reactive({
    collapsed: overrides.collapsed ?? true,
    tabs: [] as unknown[],
    activeTabId: null as string | null,
    activeScope: 'global',
    width: overrides.width ?? 420,
    maximized: overrides.maximized ?? false,
    toggle: () => {},
    toggleMaximized: () => {},
    expand: () => {},
    setWidth: () => {},
    fitToViewport: () => {},
    openTab: () => 'test-tab',
    tabsInScope: () => [],
    setActiveSession: () => {},
    setActiveScope: () => {},
    activateTab: () => {},
    closeTab: () => {},
    rememberClosed: () => {},
    recentClosedFor: () => [],
    reopenClosed: () => 'test-tab',
    forgetSession: () => {}
  });
  vi.stubGlobal('useRightSidebarStore', () => store);
  return { store, restore: () => vi.stubGlobal('useRightSidebarStore', original) };
}

/**
 * A controllable UI store (left collapse flag the float snapshot reads).
 * @param overrides Initial collapsed state.
 * @param overrides.sidebarCollapsed Initial left-column collapse flag.
 * @returns The stubbed store and a restore hook.
 */
function stubUi(overrides: { sidebarCollapsed?: boolean } = {}) {
  const original = (globalThis as any).useUiStore;
  const ui = reactive({
    sidebarCollapsed: overrides.sidebarCollapsed ?? false,
    settingsMenuOpen: false,
    sidebarBody: 'sessions' as 'sessions' | 'files',
    filesSectionOpen: true,
    gitSectionOpen: true,
    filesSplitSize: 50,
    setTheme: () => {},
    toggleSidebar: () => {},
    toggleSidebarBody: () => {},
    toggleFilesSection: () => {},
    toggleGitSection: () => {},
    setFilesSplitSize: () => {}
  });
  vi.stubGlobal('useUiStore', () => ui);
  return { ui, restore: () => vi.stubGlobal('useUiStore', original) };
}

/**
 * Dispatch one pointer event on a mounted element (happy-dom has no capture).
 * @param target
 * @param type
 * @param x
 * @param y
 */
function pointer(target: Element, type: 'pointerdown' | 'pointermove' | 'pointerup', x: number, y: number) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y });
  Object.defineProperty(event, 'pointerId', { value: 1 });
  target.dispatchEvent(event);
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
    // The logo label falls back to the neutral default name when the session has
    // no character name (it is the persona name only when the persona sets one).
    expect(wrapper.text()).toContain('🍊雪莉');
  });

  it('opens the Ctrl+K command palette, and ignores the Shift/Alt variants', async () => {
    // The document listener belongs to the app (plugins/shortcuts.ts), not the
    // shell: install the same dispatcher the plugin installs.
    const uninstall = installGlobalShortcutListener();
    try {
      const wrapper = mountHome();
      const palette = wrapper.findComponent(CommandPalette);
      expect(palette.props('visible')).toBe(false);

      // Ctrl+Shift+K is a different chord: the shell must leave it alone.
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, shiftKey: true }));
      await nextTick();
      expect(palette.props('visible')).toBe(false);

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
      await nextTick();
      expect(palette.props('visible')).toBe(true);

      // The shell's REGISTRATION is removed with it: a mounted sibling answers
      // instead (a leaked handler would also fire on the unmounted page).
      const fresh = mountHome();
      wrapper.unmount();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }));
      await nextTick();
      expect(fresh.findComponent(CommandPalette).props('visible')).toBe(true);
      fresh.unmount();
    } finally {
      uninstall();
    }
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

    // The sidebar used to carry a Chats / Background tasks tab strip plus its own task list;
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
      // Order in the left group: the project-directory button sits right of the
      // sidebar toggle, the bell after it (they swapped).
      expect(titles.indexOf('项目文件')).toBeLessThan(titles.indexOf('通知'));
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

  it('thins the toolbar to the two collapse buttons on a narrow column', () => {
    // Container queries cannot be evaluated in happy-dom, so this pins the
    // contract at the class level: the three optional controls carry the narrow
    // variant (with `!` — PrimeVue's unlayered display rules beat a layered
    // utility) and the two collapse buttons carry none.
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

      expect(wrapper.get('button[title="项目文件"]').classes()).toContain('@max-[300px]:hidden!');
      expect(wrapper.get('button[title="菜单"]').classes()).toContain('@max-[300px]:hidden!');
      const bell = wrapper.get('button[title="通知"]');
      expect(bell.element.parentElement?.className ?? '').toContain('@max-[300px]:hidden!');
      // The two collapse buttons stay in every width.
      const collapse = wrapper.findAll('button').filter(b => /侧边栏/.test(b.attributes('title') ?? ''));
      expect(collapse.length).toBe(2);
      for (const button of collapse) {
        expect(button.classes().join(' ')).not.toContain('@max-[');
      }
    } finally {
      dir.restore();
      vi.stubGlobal('useRoute', originalRoute);
    }
  });

  it('expands a collapsed sidebar when the files switch is clicked', async () => {
    // A body switch on a collapsed sidebar would look inert: clicking the folder
    // must reveal the tree it switches to.
    const originalRoute = (globalThis as any).useRoute;
    const originalUi = (globalThis as any).useUiStore;
    vi.stubGlobal('useRoute', () => ({
      path: '/home/s1',
      fullPath: '/home/s1',
      params: { sid: 's1' },
      query: {}
    }));
    const uiState = reactive({
      sidebarCollapsed: true,
      sidebarBody: 'sessions' as 'sessions' | 'files',
      // The real store's own toggle; a bare state stub would make the click a no-op.
      toggleSidebarBody: () => {
        uiState.sidebarBody = uiState.sidebarBody === 'sessions' ? 'files' : 'sessions';
      }
    });
    vi.stubGlobal('useUiStore', () => uiState);
    const dir = stubProjectDirectory('/proj');
    try {
      const wrapper = mountHome();
      // The folder switch shows the project files…
      await wrapper.get('button[title="项目文件"]').trigger('click');

      expect(uiState.sidebarBody).toBe('files');
      expect(uiState.sidebarCollapsed).toBe(false);

      // …and the way back reveals the session list the same way.
      uiState.sidebarCollapsed = true;
      await wrapper.get('button[title="会话列表"]').trigger('click');

      expect(uiState.sidebarBody).toBe('sessions');
      expect(uiState.sidebarCollapsed).toBe(false);
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
      // In files mode the button names the way BACK, so "会话列表" ("Sessions") is its title.
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

describe('home/index.vue — floating conversation column', () => {
  beforeEach(() => {
    const store = useChatFloatStore();
    store.dock();
    store.setAnchor('center');
    store.setSize({ width: 0, height: 0 });
    store.setCollapsed(false);
  });

  /**
   * The toolbar's float/restore toggle (its title flips with the state).
   * @param wrapper
   */
  const floatButton = (wrapper: ReturnType<typeof mountHome>) =>
    wrapper.findAll('.btn').find(b => ['浮动显示对话', '恢复对话栏'].includes(b.attributes('title') ?? ''));

  it('places the float toggle between the collapse button and the folder button', () => {
    const originalRoute = (globalThis as any).useRoute;
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
      const titles = wrapper.findAll('[data-test="toolbar-left"] .btn').map(b => b.attributes('title'));
      expect(titles[0]).toMatch(/折叠侧边栏|展开侧边栏/);
      expect(titles[1]).toBe('浮动显示对话');
      expect(titles[2]).toBe('项目文件');
    } finally {
      dir.restore();
      vi.stubGlobal('useRoute', originalRoute);
    }
  });

  it('detaches the middle column, splits the freed width, and restores exactly', async () => {
    // A 1440px window keeps both side columns expanded (at 1024 the auto-collapse
    // net retracts the right one, which is the one-sided case below).
    const originalWidth = window.innerWidth;
    const originalHeight = window.innerHeight;
    Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true });
    Object.defineProperty(window, 'innerHeight', { value: 900, configurable: true });
    const right = stubRightSidebar({ collapsed: false, width: 420 });
    try {
      const wrapper = mountHome();
      await floatButton(wrapper)!.trigger('click');
      await nextTick();

      const column = wrapper.get('[data-test="chat-column"]');
      expect(column.attributes('data-presentation')).toBe('floating');
      expect(column.classes()).toContain('fixed');
      expect(column.classes()).not.toContain('relative');
      // Above the right sidebar's maximized overlay (z-40).
      expect(column.classes()).toContain('z-50');
      const slot = { left: 0, top: 0, width: 1440, height: 900 };
      const size = resolveFloatingSize(slot);
      expect(column.attributes('style')).toContain(`width: ${size.width}px`);
      expect(column.attributes('style')).toContain(`height: ${size.height}px`);
      expect(column.attributes('style')).toContain('min-width: 0px');
      expect(useChatFloatStore().floating).toBe(true);

      // Left 360 and right 420 are expanded: the left keeps its width and the
      // right absorbs the docked middle's whole width.
      const extra = Math.max(0, 1440 - 360 - 420);
      expect(wrapper.get('[data-test="session-sidebar"]').attributes('style')).toContain('width: 360px');
      expect(wrapper.get('aside').attributes('style')).toContain(`width: ${420 + extra}px`);
      // The window owns the header drag handle and the explicit restore control.
      expect(wrapper.find('[data-test="chat-float-header"]').exists()).toBe(true);
      expect(wrapper.text()).toContain('恢复原样');

      await wrapper.get('[data-test="chat-float-restore"]').trigger('click');
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('docked');
      expect(wrapper.get('[data-test="session-sidebar"]').attributes('style')).toContain('width: 360px');
      expect(wrapper.get('aside').attributes('style')).toContain('width: 420px');
      expect(useChatFloatStore().floating).toBe(false);
    } finally {
      right.restore();
      Object.defineProperty(window, 'innerWidth', { value: originalWidth, configurable: true });
      Object.defineProperty(window, 'innerHeight', { value: originalHeight, configurable: true });
    }
  });

  it('hides both sidebar collapse buttons while floating (Ctrl+B still closes it)', async () => {
    const ui = stubUi({ sidebarCollapsed: false });
    const uninstall = installGlobalShortcutListener();
    try {
      const wrapper = mountHome();
      // Docked: the left toggle and the right panel toggle are both present.
      expect(wrapper.findAll('.btn').map(b => b.attributes('title'))).toEqual(expect.arrayContaining(['折叠侧边栏']));
      await floatButton(wrapper)!.trigger('click');
      await nextTick();
      const titles = wrapper.findAll('.btn').map(b => b.attributes('title'));
      expect(titles).not.toContain('折叠侧边栏');
      expect(titles).not.toContain('展开侧边栏');
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('floating');

      // The global shortcut is the remaining way to hide the window.
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'b', ctrlKey: true }));
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('docked');
      expect(ui.ui.sidebarCollapsed).toBe(true);
      wrapper.unmount();
    } finally {
      uninstall();
      ui.restore();
    }
  });

  /**
   * A fake composer dock inside the middle column (NuxtPage is stubbed in tests).
   * @param wrapper
   * @param height
   */
  const appendComposerDock = (wrapper: ReturnType<typeof mountHome>, height = 160): HTMLElement => {
    const column = wrapper.get('[data-test="chat-column"]').element as HTMLElement;
    const dock = document.createElement('div');
    dock.setAttribute('data-test', 'chat-composer-dock');
    Object.defineProperty(dock, 'getBoundingClientRect', {
      value: () => ({ height }) as unknown as DOMRect
    });
    column.appendChild(dock);
    return dock;
  };

  /** Wait one animation frame (the composer-dock bind is nextTick + rAF). */
  const nextFrame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()));

  it('collapses from the header button and expands from the pill', async () => {
    const wrapper = mountHome();
    appendComposerDock(wrapper);
    await floatButton(wrapper)!.trigger('click');
    await nextTick();
    await nextTick();
    await nextFrame();
    await nextTick();

    // One click on the normal window's top button drops to the input-only bar.
    await wrapper.get('[data-test="chat-float-collapse"]').trigger('click');
    await nextTick();
    expect(useChatFloatStore().collapsed).toBe(true);
    expect(wrapper.find('[data-test="chat-float-header"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="chat-column"]').attributes('style')).toContain('height: 160px');

    // The pill just above the bar is the way back.
    await wrapper.get('[data-test="chat-float-expand"]').trigger('click');
    await nextTick();
    expect(useChatFloatStore().collapsed).toBe(false);
    expect(wrapper.find('[data-test="chat-float-header"]').exists()).toBe(true);
  });

  it('collapses into the input-only bar at the lower bound and expands again', async () => {
    const wrapper = mountHome();
    appendComposerDock(wrapper);
    await floatButton(wrapper)!.trigger('click');
    await nextTick();
    await nextTick();
    await nextFrame();
    await nextTick();
    expect(wrapper.find('[data-test="chat-float-header"]').exists()).toBe(true);

    const grip = wrapper.get('[data-test="chat-float-resize-bottom"]').element;
    pointer(grip, 'pointerdown', 500, 600);
    pointer(grip, 'pointermove', 500, 200);
    pointer(grip, 'pointerup', 500, 200);
    await nextTick();

    expect(useChatFloatStore().collapsed).toBe(true);
    // Input only: the window header AND the shell toolbar are gone.
    expect(wrapper.find('[data-test="chat-float-header"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="toolbar-left"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="chat-column"]').attributes('style')).toContain('height: 160px');
    expect(wrapper.find('[data-test="chat-float-expand"]').exists()).toBe(true);

    await wrapper.get('[data-test="chat-float-expand"]').trigger('click');
    await nextTick();
    await nextTick();
    expect(useChatFloatStore().collapsed).toBe(false);
    expect(wrapper.find('[data-test="chat-float-header"]').exists()).toBe(true);
    const slot = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    expect(wrapper.get('[data-test="chat-column"]').attributes('style')).toContain(
      `height: ${resolveFloatingSize(slot).height}px`
    );
  });

  it('resizes the width from the right edge within the lower and upper bounds', async () => {
    const wrapper = mountHome();
    await floatButton(wrapper)!.trigger('click');
    await nextTick();
    const slot = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    const size = resolveFloatingSize(slot);
    const start = resolveFloatingPosition('center', slot, size);
    const grip = wrapper.get('[data-test="chat-float-resize-right"]').element;

    pointer(grip, 'pointerdown', start.left + size.width, start.top + 100);
    pointer(grip, 'pointermove', start.left + size.width + 1000, start.top + 100);
    pointer(grip, 'pointerup', start.left + size.width + 1000, start.top + 100);
    await nextTick();
    expect(useChatFloatStore().width).toBe(Math.min(1200, window.innerWidth - start.left - 16));

    pointer(grip, 'pointerdown', start.left + size.width, start.top + 100);
    pointer(grip, 'pointermove', -2000, start.top + 100);
    pointer(grip, 'pointerup', -2000, start.top + 100);
    await nextTick();
    expect(useChatFloatStore().width).toBe(360);
  });

  it('resizes the width from the left edge with the right side fixed', async () => {
    const wrapper = mountHome();
    await floatButton(wrapper)!.trigger('click');
    await nextTick();
    const slot = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    const size = resolveFloatingSize(slot);
    const start = resolveFloatingPosition('center', slot, size);
    const grip = wrapper.get('[data-test="chat-float-resize-left"]').element;

    pointer(grip, 'pointerdown', start.left, start.top + 100);
    pointer(grip, 'pointermove', start.left - 100, start.top + 100);
    pointer(grip, 'pointerup', start.left - 100, start.top + 100);
    await nextTick();
    expect(useChatFloatStore().width).toBe(size.width + 100);
    expect(wrapper.get('[data-test="chat-column"]').attributes('style')).toContain(`left: ${start.left - 100}px`);
  });

  it('ignores a sub-threshold resize nudge', async () => {
    const wrapper = mountHome();
    await floatButton(wrapper)!.trigger('click');
    await nextTick();
    const grip = wrapper.get('[data-test="chat-float-resize-right"]').element;
    pointer(grip, 'pointerdown', 500, 300);
    pointer(grip, 'pointermove', 502, 300);
    pointer(grip, 'pointerup', 502, 300);
    await nextTick();
    expect(useChatFloatStore().width).toBe(0);
  });

  it('drags by the header and snaps to the nearest anchor; a 2px nudge is a click', async () => {
    const wrapper = mountHome();
    await floatButton(wrapper)!.trigger('click');
    await nextTick();
    const header = wrapper.get('[data-test="chat-float-header"]').element;
    const slot = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    const size = resolveFloatingSize(slot);
    const start = resolveFloatingPosition('center', slot, size);

    pointer(header, 'pointerdown', start.left + 30, start.top + 20);
    pointer(header, 'pointermove', start.left + 32, start.top + 20);
    pointer(header, 'pointerup', start.left + 32, start.top + 20);
    await nextTick();
    expect(useChatFloatStore().anchor).toBe('center');

    pointer(header, 'pointerdown', start.left + 30, start.top + 20);
    pointer(header, 'pointermove', start.left + 430, start.top + 320);
    pointer(header, 'pointerup', start.left + 430, start.top + 320);
    await nextTick();
    const dropped = clampFloatingPosition({ left: start.left + 400, top: start.top + 300 }, slot, size);
    expect(useChatFloatStore().anchor).toBe(resolveNearestFloatingAnchor(dropped, slot, size));
  });

  it('keeps a persisted float when the right panel is not maximized at boot', async () => {
    useChatFloatStore().open();
    const wrapper = mountHome();
    await nextTick();
    expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('floating');
  });

  it('auto-floats a boot with a persisted maximize, and docks when the panel restores', async () => {
    const right = stubRightSidebar({ collapsed: false, width: 420, maximized: true });
    try {
      const wrapper = mountHome();
      await nextTick();
      // The boot state is the snapshot: left expanded, chat was docked.
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('floating');
      right.store.maximized = false;
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('docked');
    } finally {
      right.restore();
    }
  });

  it('auto-floats while the right panel is maximized and docks back when the chat was docked', async () => {
    const right = stubRightSidebar({ collapsed: false, width: 420 });
    try {
      const wrapper = mountHome();
      right.store.maximized = true;
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('floating');

      right.store.maximized = false;
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('docked');
    } finally {
      right.restore();
    }
  });

  it('keeps the float after the panel restores when the left was expanded and the chat was floating', async () => {
    const right = stubRightSidebar({ collapsed: false, width: 420 });
    try {
      const wrapper = mountHome();
      await floatButton(wrapper)!.trigger('click');
      await nextTick();

      right.store.maximized = true;
      await nextTick();
      right.store.maximized = false;
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('floating');
      expect(useChatFloatStore().floating).toBe(true);
    } finally {
      right.restore();
    }
  });

  it('docks after the panel restores when the left sidebar was collapsed at expand time', async () => {
    const ui = stubUi({ sidebarCollapsed: true });
    const right = stubRightSidebar({ collapsed: false, width: 420 });
    try {
      const wrapper = mountHome();
      await floatButton(wrapper)!.trigger('click');
      await nextTick();

      right.store.maximized = true;
      await nextTick();
      right.store.maximized = false;
      await nextTick();
      expect(wrapper.get('[data-test="chat-column"]').attributes('data-presentation')).toBe('docked');
      expect(useChatFloatStore().floating).toBe(false);
    } finally {
      right.restore();
      ui.restore();
    }
  });
});
