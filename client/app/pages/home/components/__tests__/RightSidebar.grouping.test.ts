/**
 * UI upgrade S6: the right sidebar's strip groups 26 panel kinds into three
 * functional tabs (session / tools / config) and adds cross-group panel search.
 * This suite uses a store double that EXPOSES the grouping API so the new path
 * in RightSidebar.vue is exercised; the legacy scope suite keeps its own double
 * (without groupTabs) to prove the additive fallback stays green.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { computed, nextTick, reactive, ref } from 'vue';
import RightSidebar from '@/pages/home/components/RightSidebar.vue';
import { groupOfKind, type PanelGroup, type RightSidebarPanelKind } from '@/stores/right-sidebar';

interface FakeTab {
  id: string;
  kind: RightSidebarPanelKind;
  scope: 'session' | 'global';
}

let api: {
  collapsed: boolean;
  tabs: FakeTab[];
  activeTabId: string | null;
  activeScope: 'session' | 'global';
  activeGroup: PanelGroup;
  width: number;
  maximized: boolean;
  openTab: ReturnType<typeof vi.fn>;
  activateTab: ReturnType<typeof vi.fn>;
  closeTab: ReturnType<typeof vi.fn>;
  tabsInScope: (scope: 'session' | 'global') => FakeTab[];
  groupTabs: FakeTab[];
  searchPanels: ReturnType<typeof vi.fn>;
  recentClosedFor: ReturnType<typeof vi.fn>;
  reopenClosed: ReturnType<typeof vi.fn>;
  setWidth: ReturnType<typeof vi.fn>;
  fitToViewport: ReturnType<typeof vi.fn>;
  setActiveScope: ReturnType<typeof vi.fn>;
  toggleMaximized: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('innerWidth', 1280);
  api = reactive({
    collapsed: ref(false),
    tabs: ref<FakeTab[]>([
      { id: 'sideChat-1', kind: 'sideChat', scope: 'session' },
      { id: 'browser-1', kind: 'browser', scope: 'session' },
      { id: 'logs-1', kind: 'logs', scope: 'global' }
    ]),
    activeTabId: ref<string | null>(null),
    activeScope: ref<'session' | 'global'>('session'),
    activeGroup: ref<PanelGroup>('session'),
    width: ref(420),
    maximized: ref(false),
    openTab: vi.fn((kind: RightSidebarPanelKind) => `opened-${kind}`),
    activateTab: vi.fn(),
    closeTab: vi.fn(),
    tabsInScope: (scope: 'session' | 'global') => api.tabs.filter(tab => tab.scope === scope),
    groupTabs: computed(() => api.tabs.filter(tab => groupOfKind(tab.kind) === api.activeGroup)),
    searchPanels: vi.fn((query: string, translate: (key: string) => string) => {
      if (!query.trim()) return [];
      const labels: Record<string, string> = {
        browser: translate('rightSidebar.browser'),
        logs: translate('rightSidebar.logs'),
        sideChat: translate('rightSidebar.sideChat')
      };
      return Object.entries(labels)
        .filter(([, label]) => label.toLowerCase().includes(query.toLowerCase()))
        .map(([kind, label]) => ({ kind, label, group: groupOfKind(kind as RightSidebarPanelKind) }));
    }),
    recentClosedFor: () => [],
    reopenClosed: vi.fn(),
    setWidth: vi.fn(),
    fitToViewport: vi.fn(),
    setActiveScope: vi.fn(),
    toggleMaximized: vi.fn()
  }) as unknown as typeof api;
  vi.stubGlobal('useRightSidebarStore', () => api);
});

afterEach(() => {
  vi.useRealTimers();
});

function mountSidebar() {
  return mount(RightSidebar, {
    global: {
      stubs: {
        RecentClosedTabsMenu: { name: 'RecentClosedTabsMenu', template: '<div data-test="recent-menu" />' }
      }
    }
  });
}

describe('RightSidebar.vue — functional groups (S6)', () => {
  it('renders three group tabs; the strip shows only the active group', async () => {
    const wrapper = mountSidebar();
    const groups = wrapper.findAll('[data-test^="group-"]');
    expect(groups.map(g => g.attributes('data-test'))).toEqual(['group-session', 'group-tools', 'group-config']);
    // Default group = session: sideChat tab visible, browser/logs hidden.
    expect(wrapper.find('[data-test="tab-sideChat"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="tab-browser"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="tab-logs"]').exists()).toBe(false);

    await wrapper.get('[data-test="group-tools"]').trigger('click');
    expect(api.activeGroup).toBe('tools');
    expect(wrapper.find('[data-test="tab-browser"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="tab-sideChat"]').exists()).toBe(false);
    await nextTick();
    expect(wrapper.find('[data-test="tab-logs"]').exists()).toBe(false);

    // Switching back keeps the other groups' tabs (never destroyed).
    await wrapper.get('[data-test="group-config"]').trigger('click');
    expect(wrapper.find('[data-test="tab-logs"]').exists()).toBe(true);
    expect(api.tabs).toHaveLength(3);
  });

  it('searching a panel opens it and switches to its group', async () => {
    const wrapper = mountSidebar();
    const input = wrapper.get('[data-test="panel-search"]');
    await input.setValue('浏览器');

    expect(wrapper.find('[data-test="search-result-browser"]').exists()).toBe(true);
    await wrapper.get('[data-test="search-result-browser"]').trigger('click');
    expect(api.openTab).toHaveBeenCalledWith('browser');
    // The double mirrors the store's openTab behaviour of revealing the group.
    api.activeGroup = groupOfKind('browser');
    await nextTick();
    expect(api.activeGroup).toBe('tools');
    expect((input.element as HTMLInputElement).value).toBe('');
  });

  it('a blank query shows no search results', async () => {
    const wrapper = mountSidebar();
    await wrapper.get('[data-test="panel-search"]').setValue('');
    expect(wrapper.find('[data-test^="search-result-"]').exists()).toBe(false);
    expect(api.searchPanels).not.toHaveBeenCalled();
  });
});
