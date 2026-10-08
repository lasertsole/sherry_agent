/**
 * The left sidebar's 工作目录 body: the two section headers (文件树 / Git Graph)
 * share ONE row in EVERY open/close combination, and the Splitter exists ONLY
 * while both panels do.
 *
 * That second rule is a bug fix, not taste: PrimeVue's Splitter keeps references
 * to its panels and logged `[dev error] Splitter happened error... Cannot read
 * properties of undefined (reading 'style')` when a `v-if` panel disappeared
 * under a live instance — which is exactly what happened when the user toggled
 * the two sections quickly. A one-panel splitter is never rendered now, so the
 * panel set of a mounted Splitter cannot change.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive } from 'vue';
import SessionSidebar from '@/pages/home/components/SessionSidebar.vue';

// Auto-imported collaborator the component reaches for on mount (and the event
// bus it subscribes to); the suite's shared setup covers the stores.
vi.stubGlobal(
  'getSessionList',
  vi.fn(async () => [])
);
vi.stubGlobal(
  'readCachedSessionMetaList',
  vi.fn(async () => [])
);
vi.stubGlobal('on', vi.fn());
vi.stubGlobal('off', vi.fn());
vi.stubGlobal('useRoute', () => ({ params: { sid: 'sid-1' } }));
vi.stubGlobal('useRouter', () => ({ push: vi.fn() }));
vi.stubGlobal('useLocalePath', () => (path: string) => path);

/** The ui store stand-in: only the working-directory section state matters here. */
function makeUiStore() {
  const ui = reactive({
    sidebarBody: 'files',
    sidebarCollapsed: false,
    filesSectionOpen: true,
    gitSectionOpen: true,
    filesSplitSize: 50,
    toggleFilesSection: () => {
      ui.filesSectionOpen = !ui.filesSectionOpen;
    },
    toggleGitSection: () => {
      ui.gitSectionOpen = !ui.gitSectionOpen;
    },
    setFilesSplitSize: (value: number) => {
      ui.filesSplitSize = value;
    },
    toggleSidebar: () => {},
    toggleSidebarBody: () => {}
  });
  return ui;
}

const stubs = {
  HistoryItem: true,
  ProjectFileTree: { name: 'ProjectFileTree', props: ['sessionId'], template: '<div />' },
  GitGraphPanel: { name: 'GitGraphPanel', props: ['sessionId'], template: '<div />' },
  Splitter: { name: 'Splitter', template: '<div data-test="sidebar-splitter"><slot /></div>' },
  SplitterPanel: { name: 'SplitterPanel', template: '<div data-test="splitter-panel"><slot /></div>' },
  // PrimeVue widgets of the SESSIONS body (rendered too, though this suite is
  // about the working-directory body).
  Button: { name: 'Button', template: '<button><slot /></button>' },
  InputText: { name: 'InputText', template: '<input />' },
  Calendar: { name: 'Calendar', template: '<div />' },
  Checkbox: { name: 'Checkbox', template: '<div />' }
};

let ui: ReturnType<typeof makeUiStore>;

async function mountSidebar() {
  ui = makeUiStore();
  vi.stubGlobal('useUiStore', () => ui);
  const wrapper = mount(SessionSidebar, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

/**
 * The header row is always horizontal: one flex row, never a column.
 * @param wrapper
 */
function headerRowClass(wrapper: Awaited<ReturnType<typeof mountSidebar>>): string {
  const toggle = wrapper.get('[data-test="sidebar-files-toggle"]');
  const row = toggle.element.parentElement;
  expect(row?.contains(wrapper.get('[data-test="sidebar-git-toggle"]').element)).toBe(true);
  return row?.className ?? '';
}

describe('SessionSidebar working-directory sections', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('keeps both headers on one horizontal row while both panels are open', async () => {
    const wrapper = await mountSidebar();

    const row = headerRowClass(wrapper);
    expect(row).toContain('flex');
    expect(row).not.toContain('flex-col');
    expect(wrapper.findAll('[data-test="splitter-panel"]')).toHaveLength(2);
  });

  it('drops the Splitter as soon as one section closes, rendering a plain container', async () => {
    const wrapper = await mountSidebar();

    await wrapper.get('[data-test="sidebar-git-toggle"]').trigger('click');

    expect(wrapper.find('[data-test="sidebar-splitter"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="sidebar-files-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="sidebar-git-panel"]').exists()).toBe(false);
    // The headers still share the row with one panel gone.
    expect(headerRowClass(wrapper)).not.toContain('flex-col');
  });

  it('renders the git panel alone when only it is open, still without a Splitter', async () => {
    const wrapper = await mountSidebar();

    await wrapper.get('[data-test="sidebar-files-toggle"]').trigger('click');

    expect(wrapper.find('[data-test="sidebar-splitter"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="sidebar-git-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="sidebar-files-panel"]').exists()).toBe(false);
    expect(headerRowClass(wrapper)).not.toContain('flex-col');
  });

  it('renders no panel with both sections collapsed, and the headers keep the row', async () => {
    const wrapper = await mountSidebar();

    await wrapper.get('[data-test="sidebar-files-toggle"]').trigger('click');
    await wrapper.get('[data-test="sidebar-git-toggle"]').trigger('click');

    expect(wrapper.find('[data-test="sidebar-splitter"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="sidebar-files-panel"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="sidebar-git-panel"]').exists()).toBe(false);
    expect(headerRowClass(wrapper)).not.toContain('flex-col');
  });

  it('honours a fast toggle burst: the Splitter is present exactly when both are open', async () => {
    const wrapper = await mountSidebar();

    // No awaits between the clicks: the same interleaving that produced the
    // PrimeVue error live (the invariant must hold after every step).
    for (const [filesOpen, gitOpen] of [
      [false, false],
      [false, true],
      [true, true],
      [true, false],
      [false, false]
    ] as const) {
      while (ui.filesSectionOpen !== filesOpen) {
        await wrapper.get('[data-test="sidebar-files-toggle"]').trigger('click');
      }
      while (ui.gitSectionOpen !== gitOpen) {
        await wrapper.get('[data-test="sidebar-git-toggle"]').trigger('click');
      }
      expect(wrapper.find('[data-test="sidebar-splitter"]').exists()).toBe(filesOpen && gitOpen);
    }
  });
});
