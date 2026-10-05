import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { nextTick, reactive, ref } from 'vue';
import RightSidebar from '@/pages/home/components/RightSidebar.vue';
import { RIGHT_SIDEBAR_PANEL_MIN_HEIGHT, RIGHT_SIDEBAR_PANEL_MIN_WIDTH } from '@/stores/right-sidebar';

// The store is a Nuxt auto-import: shadow it with a real-shaped double so the
// strip's rendering and the tab actions can be asserted directly. `reactive`
// auto-unwraps the nested refs exactly like a Pinia store does, so the
// component reads `store.tabs` as an array while the test mutates the refs.
let sidebarApi: {
  collapsed: boolean;
  tabs: Array<{ id: string; kind: string; scope: 'session' | 'global' }>;
  activeTabId: string | null;
  activeScope: 'session' | 'global';
  width: number;
  toggle: ReturnType<typeof vi.fn>;
  expand: ReturnType<typeof vi.fn>;
  setWidth: ReturnType<typeof vi.fn>;
  fitToViewport: ReturnType<typeof vi.fn>;
  openTab: ReturnType<typeof vi.fn>;
  tabsInScope: (scope: 'session' | 'global') => Array<{ id: string; kind: string; scope: string }>;
  setActiveScope: (scope: 'session' | 'global') => void;
  activateTab: ReturnType<typeof vi.fn>;
  closeTab: ReturnType<typeof vi.fn>;
};

const VIEWPORT_WIDTH = 1280;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('innerWidth', VIEWPORT_WIDTH);
  sidebarApi = reactive({
    collapsed: ref(true),
    tabs: ref<Array<{ id: string; kind: string; scope: 'session' | 'global' }>>([]),
    activeTabId: ref<string | null>(null),
    activeScope: ref<'session' | 'global'>('global'),
    width: ref(420),
    toggle: vi.fn(),
    expand: vi.fn(),
    setWidth: vi.fn(),
    fitToViewport: vi.fn(),
    openTab: vi.fn(),
    tabsInScope: scope => sidebarApi.tabs.filter(tab => tab.scope === scope),
    setActiveScope: vi.fn(scope => {
      sidebarApi.activeScope = scope;
    }),
    activateTab: vi.fn(),
    closeTab: vi.fn()
  }) as typeof sidebarApi;
  vi.stubGlobal('useRightSidebarStore', () => sidebarApi);
});

afterEach(() => {
  vi.useRealTimers();
});

// Panels are lazily imported: stub all three modules so the strip can be tested
// without mounting the heavy log/statistics bodies.
vi.mock('@/pages/home/components/LogsPanel.vue', () => ({
  __esModule: true,
  default: { name: 'LogsPanelStub', template: '<div data-test="logs-panel">logs</div>' }
}));
vi.mock('@/pages/home/components/StatsPanel.vue', () => ({
  __esModule: true,
  default: { name: 'StatsPanelStub', template: '<div data-test="stats-panel">stats</div>' }
}));
vi.mock('@/pages/home/components/KnowledgeGraphPanel.vue', () => ({
  __esModule: true,
  default: { name: 'KgPanelStub', template: '<div data-test="kg-panel">kg</div>' }
}));
vi.mock('@/pages/home/components/SkillsPanel.vue', () => ({
  __esModule: true,
  default: { name: 'SkillsPanelStub', emits: ['saved'], template: '<div data-test="skills-panel">skills</div>' }
}));
vi.mock('@/pages/home/components/ConfigPanel.vue', () => ({
  __esModule: true,
  default: { name: 'ConfigPanelStub', emits: ['saved'], template: '<div data-test="config-panel">config</div>' }
}));
vi.mock('@/pages/home/components/PersonaPanel.vue', () => ({
  __esModule: true,
  default: { name: 'PersonaPanelStub', emits: ['saved'], template: '<div data-test="persona-panel">persona</div>' }
}));
vi.mock('@/pages/home/components/MemoryPanel.vue', () => ({
  __esModule: true,
  default: { name: 'MemoryPanelStub', emits: ['saved'], template: '<div data-test="memory-panel">memory</div>' }
}));
vi.mock('@/pages/home/components/HeartbeatPanel.vue', () => ({
  __esModule: true,
  default: {
    name: 'HeartbeatPanelStub',
    emits: ['saved'],
    template: '<div data-test="heartbeat-panel">heartbeat</div>'
  }
}));
vi.mock('@/pages/home/components/CronPanel.vue', () => ({
  __esModule: true,
  default: { name: 'CronPanelStub', emits: ['saved'], template: '<div data-test="cron-panel">cron</div>' }
}));
vi.mock('@/pages/home/components/ExtendPanel.vue', () => ({
  __esModule: true,
  default: { name: 'ExtendPanelStub', emits: ['saved'], template: '<div data-test="extend-panel">extend</div>' }
}));

const stubs = {
  Button: {
    name: 'Button',
    props: ['icon', 'title', 'ariaLabel', 'variant', 'size'],
    emits: ['click'],
    template: '<button class="btn" :title="title" @click="$emit(\'click\', $event)"><i :class="icon"></i></button>'
  }
};

function mountSidebar() {
  return mount(RightSidebar, { global: { stubs } });
}

describe('RightSidebar.vue (integration, store mocked)', () => {
  it('renders the collapsed shell with no body at all', () => {
    const wrapper = mountSidebar();

    expect(wrapper.find('aside').classes()).toContain('w-0');
    // Nothing of the body is rendered while retracted — that is what closes the
    // panel's live streams.
    expect(wrapper.text()).toBe('');
    expect(wrapper.find('aside [role="separator"]').exists()).toBe(false);
  });

  it('expands to the store width (inline style, drag-resizable)', () => {
    sidebarApi.collapsed = false;
    sidebarApi.width = 512;
    const wrapper = mountSidebar();

    expect(wrapper.find('aside').attributes('style')).toContain('width: 512px');
    // The body keeps the expanded width too, so the shell animation clips it
    // instead of squeezing its contents.
    expect(wrapper.find('aside > div:last-child').attributes('style')).toContain('width: 512px');
    // Collapsed renders no inline width at all (the w-0 class rules).
    sidebarApi.collapsed = true;
    const collapsedWrapper = mountSidebar();
    expect(collapsedWrapper.find('aside').attributes('style')).toBeUndefined();
  });

  it('keeps the body mounted through the collapse animation, then drops it', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [{ id: 'logs-1', kind: 'logs', scope: 'global' }];
    sidebarApi.activeTabId = 'logs-1';
    const wrapper = mountSidebar();
    await flushPromises();
    expect(wrapper.find('[data-test="logs-panel"]').exists()).toBe(true);

    sidebarApi.collapsed = true;
    await nextTick();
    // Mid-animation: the shell is already w-0 but the body is still there to
    // slide out with it.
    expect(wrapper.find('aside').classes()).toContain('w-0');
    expect(wrapper.find('[data-test="logs-panel"]').exists()).toBe(true);

    vi.advanceTimersByTime(400);
    await nextTick();
    expect(wrapper.find('[data-test="logs-panel"]').exists()).toBe(false);
  });

  it('renders one strip button per tab and activates on click', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [
      { id: 'logs-1', kind: 'logs', scope: 'global' },
      { id: 'stats-1', kind: 'stats', scope: 'global' }
    ];
    sidebarApi.activeTabId = 'logs-1';
    const wrapper = mountSidebar();

    const stripButtons = wrapper.findAll('button.group');
    expect(stripButtons.map(b => b.text())).toEqual(['日志查看', '统计']);
    // The active tab is highlighted; the panel is an async component, so let it resolve.
    expect(stripButtons[0]!.classes()).toContain('text-theme-main');
    await flushPromises();
    expect(wrapper.find('[data-test="logs-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="stats-panel"]').exists()).toBe(false);

    await stripButtons[1]!.trigger('click');
    expect(sidebarApi.activateTab).toHaveBeenCalledWith('stats-1');
  });

  it('splits the strip into 当前会话 / 全局 group tabs', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [
      { id: 'sessionPreset-1', kind: 'sessionPreset', scope: 'session' },
      { id: 'logs-1', kind: 'logs', scope: 'global' }
    ];
    sidebarApi.activeTabId = 'logs-1';
    const wrapper = mountSidebar();
    await flushPromises();

    // 全局 is the default group: the pre-existing tools are unchanged by grouping.
    const scopeTabs = wrapper.findAll('[data-test^="scope-tab-"]');
    expect(scopeTabs.map(tab => tab.attributes('data-test'))).toEqual(['scope-tab-session', 'scope-tab-global']);
    // Only the active group's tabs render.
    expect(wrapper.text()).toContain('日志查看');
    expect(wrapper.text()).not.toContain('会话预设');

    await wrapper.get('[data-test="scope-tab-session"]').trigger('click');
    await flushPromises();
    expect(sidebarApi.setActiveScope).toHaveBeenCalledWith('session');
    expect(wrapper.text()).toContain('会话预设');
    expect(wrapper.text()).not.toContain('日志查看');

    // An empty group says so instead of showing nothing at all.
    sidebarApi.tabs = [{ id: 'logs-1', kind: 'logs', scope: 'global' }];
    await nextTick();
    expect(wrapper.get('[data-test="scope-empty"]').exists()).toBe(true);
  });

  it('switches the mounted panel with the active tab', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [
      { id: 'logs-1', kind: 'logs', scope: 'global' },
      { id: 'stats-1', kind: 'stats', scope: 'global' }
    ];
    sidebarApi.activeTabId = 'stats-1';
    const wrapper = mountSidebar();

    await flushPromises();
    expect(wrapper.find('[data-test="stats-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="logs-panel"]').exists()).toBe(false);
  });

  it('closes a tab from its × without activating it', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [{ id: 'logs-1', kind: 'logs', scope: 'global' }];
    sidebarApi.activeTabId = 'logs-1';
    const wrapper = mountSidebar();

    await wrapper.find('button.group i.pi-times').trigger('click');

    expect(sidebarApi.closeTab).toHaveBeenCalledWith('logs-1');
    expect(sidebarApi.activateTab).not.toHaveBeenCalled();
  });

  it('carries no add / collapse controls: both live in the top toolbar', () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [{ id: 'logs-1', kind: 'logs', scope: 'global' }];
    sidebarApi.activeTabId = 'logs-1';
    const wrapper = mountSidebar();

    expect(wrapper.find('i.pi-plus').exists()).toBe(false);
    expect(wrapper.find('i.pi-angle-double-right').exists()).toBe(false);
    // The empty state points at those two entries instead of a local + button.
    sidebarApi.tabs = [];
    sidebarApi.activeTabId = null;
    expect(mountSidebar().text()).toContain('从菜单或工具栏添加');
  });

  it('labels every tool tab from the same kind vocabulary', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [
      { id: 'skills-1', kind: 'skills', scope: 'global' },
      { id: 'systemConfig-1', kind: 'systemConfig', scope: 'global' },
      { id: 'cron-1', kind: 'cron', scope: 'global' }
    ];
    sidebarApi.activeTabId = 'skills-1';
    const wrapper = mountSidebar();

    expect(wrapper.findAll('button.group').map(b => b.text())).toEqual(['技能', '系统配置', '定时任务']);
    await flushPromises();
    expect(wrapper.find('[data-test="skills-panel"]').exists()).toBe(true);
  });

  it('relays a panel save to the shell', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [{ id: 'systemConfig-1', kind: 'systemConfig', scope: 'global' }];
    sidebarApi.activeTabId = 'systemConfig-1';
    const wrapper = mountSidebar();
    await flushPromises();

    // The shell re-reads the session's character snapshot on this event.
    wrapper.findComponent({ name: 'ConfigPanelStub' }).vm.$emit('saved');
    expect(wrapper.emitted('saved')).toHaveLength(1);
  });

  it('renders the knowledge-graph panel for its tab', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [{ id: 'kg-1', kind: 'knowledgeGraph', scope: 'global' }];
    sidebarApi.activeTabId = 'kg-1';
    const wrapper = mountSidebar();

    await flushPromises();
    expect(wrapper.find('[data-test="kg-panel"]').exists()).toBe(true);
  });

  it('scrolls the panel body on both axes below the panel minimum size', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.tabs = [{ id: 'stats-1', kind: 'stats', scope: 'global' }];
    sidebarApi.activeTabId = 'stats-1';
    const wrapper = mountSidebar();

    await flushPromises();
    const body = wrapper.find('[data-test="stats-panel"]').element.parentElement!;
    expect(body.className).toContain('overflow-auto');
    const style = wrapper.find('[data-test="stats-panel"]').attributes('style')!;
    expect(style).toContain(`min-width: ${RIGHT_SIDEBAR_PANEL_MIN_WIDTH}px`);
    expect(style).toContain(`min-height: ${RIGHT_SIDEBAR_PANEL_MIN_HEIGHT}px`);
  });

  it('marks the edge as draggable so the affordance is visible', () => {
    sidebarApi.collapsed = false;
    const wrapper = mountSidebar();

    const handle = wrapper.find('[role="separator"]');
    // A bare edge reads as a border: the grip pill, the resize cursor and the
    // tooltip are what tell the user the panel can be dragged.
    expect(handle.classes()).toContain('cursor-col-resize');
    expect(handle.attributes('title')).toBe('拖动调整宽度');
    const grip = handle.find('span');
    expect(grip.exists()).toBe(true);
    expect(grip.classes()).toContain('bg-gray-400/70');
    expect(grip.classes()).toContain('group-hover:bg-gray-400');
  });

  it('widens while the handle is dragged left and stops on pointerup', async () => {
    sidebarApi.collapsed = false;
    const wrapper = mountSidebar();
    const handle = wrapper.find('[role="separator"]');
    expect(handle.exists()).toBe(true);

    // The panel is right-anchored: dragging left (dx < 0) widens it. The clamp
    // sees the live viewport so the drag can never outgrow the window.
    await handle.trigger('pointerdown', { clientX: 800 });
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 700 }));
    expect(sidebarApi.setWidth).toHaveBeenLastCalledWith(520, VIEWPORT_WIDTH);

    // While dragging the grip highlights (mode-independent grey: the theme
    // accent is white in dark mode and vanished over a light panel).
    expect(handle.find('span').classes()).toContain('bg-gray-500');

    window.dispatchEvent(new MouseEvent('pointerup'));
    sidebarApi.setWidth.mockClear();
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 400 }));
    expect(sidebarApi.setWidth).not.toHaveBeenCalled();
  });

  it('re-clamps the stored width when the window is resized', async () => {
    sidebarApi.collapsed = false;
    sidebarApi.width = 700;
    mountSidebar();

    window.dispatchEvent(new Event('resize'));

    expect(sidebarApi.fitToViewport).toHaveBeenCalledWith(VIEWPORT_WIDTH);
  });
});
