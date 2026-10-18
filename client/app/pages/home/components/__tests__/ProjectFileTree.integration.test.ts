/**
 * The left sidebar's project file tree (lazy, session-scoped).
 *
 * Contract: the tree lists ONE level at a time, expanding a directory fetches
 * its children lazily, the per-level cap surfaces its "showing N of M" hint, and
 * a refusal renders in place instead of silently staying empty.
 *
 * The tree is only reachable for a session with a project directory bound (the
 * toolbar hides the switch otherwise), so there is no unbound state to render.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import ProjectFileTree from '@/pages/home/components/ProjectFileTree.vue';

const bridge = vi.hoisted(() => ({ fetchProjectTree: vi.fn(), fetchProjectFile: vi.fn() }));
vi.mock('~/composables/bridge', () => bridge);

const clipboard = vi.hoisted(() => ({ copyTextToClipboard: vi.fn(async () => true) }));
vi.mock('~/composables/use-message-copy', () => clipboard);

const toasts = vi.hoisted(() => ({ toastSuccess: vi.fn(), toastInfo: vi.fn(), toastError: vi.fn() }));
vi.mock('~/composables/toast', () => toasts);

const desktop = vi.hoisted(() => ({
  canUseDesktopFileActions: vi.fn(() => false),
  desktopPlatform: vi.fn(() => 'linux' as const),
  openPathWithSystemApp: vi.fn(async () => true),
  revealPathInFileManager: vi.fn(async () => true)
}));
vi.mock('~/utils/desktop-file-actions', () => desktop);

const bus = vi.hoisted(() => ({ emit: vi.fn() }));
vi.mock('~/composables/mitt', () => bus);

const dirState = vi.hoisted(() => ({ directory: '/proj' as string | null }));

const rightSidebarBridge = vi.hoisted(() => ({ openTab: vi.fn() }));

/** Every ContextMenu.show(event) the tree triggered. */
const menuShots: Event[] = [];

const stubs = {
  // Functional recursive stub: renders each node as a button (self-referencing
  // so nested children appear) and emits the parent's toggle/open events.
  FileTreeNode: {
    name: 'FileTreeNode',
    props: ['node', 'depth', 'sessionId', 'loadingPaths'],
    emits: ['toggle', 'open', 'menu'],
    template: `
      <div>
        <button
          class="node"
          :data-test="'file-node-' + node.path"
          @click="$emit(node.type === 'dir' ? 'toggle' : 'open', node)"
          @contextmenu.prevent="$emit('menu', $event, node)">{{ node.name }}</button>
        <FileTreeNode
          v-for="child in node.children"
          :key="child.path"
          :node="child"
          :depth="depth + 1"
          :session-id="sessionId"
          :loading-paths="loadingPaths"
          @toggle="n => $emit('toggle', n)"
          @open="n => $emit('open', n)"
          @menu="(e, n) => $emit('menu', e, n)" />
      </div>`
  },
  ContextMenu: {
    name: 'ContextMenu',
    props: ['model'],
    setup() {
      return { show: (event: Event) => menuShots.push(event) };
    },
    template: '<div data-test="file-tree-menu" />'
  },
  Button: {
    props: ['label', 'icon', 'title'],
    emits: ['click'],
    template: '<button class="btn" :title="title" @click="$emit(\'click\')">{{ label }}</button>'
  },
  ProgressSpinner: { template: '<div class="spinner" />' }
};

/**
 * Mount with the directory store reporting a bound project.
 * @param sessionId
 */
async function mountTree(sessionId = 'sid-1') {
  const wrapper = mount(ProjectFileTree, {
    props: { sessionId },
    global: { stubs }
  });
  await flushPromises();
  return wrapper;
}

/** Clear every mock the tree touches (both describes run the same harness). */
function resetHarness(): void {
  setActivePinia(createTestingPinia({ stubActions: false }));
  menuShots.length = 0;
  clipboard.copyTextToClipboard.mockClear().mockResolvedValue(true);
  toasts.toastSuccess.mockClear();
  toasts.toastInfo.mockClear();
  toasts.toastError.mockClear();
  desktop.canUseDesktopFileActions.mockReset().mockReturnValue(false);
  desktop.desktopPlatform.mockReset().mockReturnValue('linux');
  desktop.openPathWithSystemApp.mockClear().mockResolvedValue(true);
  desktop.revealPathInFileManager.mockClear().mockResolvedValue(true);
  bus.emit.mockClear();
  rightSidebarBridge.openTab.mockClear();
  bridge.fetchProjectFile.mockReset();
  bridge.fetchProjectTree.mockReset();
  bridge.fetchProjectTree.mockResolvedValue({
    sessionId: 'sid-1',
    root: '/proj',
    path: '',
    entries: [
      { name: 'src', type: 'dir' },
      { name: 'README.md', type: 'file', size: 10 }
    ],
    truncated: false,
    total: 2
  });
  dirState.directory = '/proj';
  vi.stubGlobal('useProjectDirectoryStore', () => ({
    stateFor: () => ({
      directory: dirState.directory,
      effective: dirState.directory ?? '/repo',
      source: dirState.directory ? 'session' : 'default',
      pendingDirectory: null,
      error: null
    }),
    hydrate: async () => {},
    clearError: () => {}
  }));
  vi.stubGlobal('useRightSidebarStore', () => ({ openTab: rightSidebarBridge.openTab }));
}

describe('ProjectFileTree', () => {
  beforeEach(() => {
    resetHarness();
  });

  it('lists the root level with directories and files', async () => {
    const wrapper = await mountTree();

    expect(bridge.fetchProjectTree).toHaveBeenCalledWith('sid-1', '');
    expect(wrapper.find('[data-test="file-node-src"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="file-node-README.md"]').exists()).toBe(true);
  });

  it('fetches one level when a directory expands', async () => {
    bridge.fetchProjectTree
      .mockResolvedValueOnce({
        sessionId: 'sid-1',
        root: '/proj',
        path: '',
        entries: [{ name: 'src', type: 'dir' }],
        truncated: false,
        total: 1
      })
      .mockResolvedValueOnce({
        sessionId: 'sid-1',
        root: '/proj',
        path: 'src',
        entries: [{ name: 'main.py', type: 'file', size: 5 }],
        truncated: false,
        total: 1
      });
    const wrapper = await mountTree();

    await wrapper.get('[data-test="file-node-src"]').trigger('click');
    await flushPromises();

    expect(bridge.fetchProjectTree).toHaveBeenCalledWith('sid-1', 'src');
    expect(wrapper.find('[data-test="file-node-src/main.py"]').exists()).toBe(true);
  });

  it('shows the truncation hint', async () => {
    bridge.fetchProjectTree.mockResolvedValue({
      sessionId: 'sid-1',
      root: '/proj',
      path: '',
      entries: [{ name: 'a.txt', type: 'file' }],
      truncated: true,
      total: 900
    });

    const wrapper = await mountTree();

    expect(wrapper.text()).toContain('900');
  });

  it('renders a refusal in place', async () => {
    bridge.fetchProjectTree.mockRejectedValue(new Error('path escapes the project directory'));

    const wrapper = await mountTree();

    expect(wrapper.get('[data-test="project-tree-error"]').text()).toContain('escapes');
  });

  it('opens a file in a right-sidebar tab', async () => {
    const wrapper = await mountTree();

    await wrapper.get('[data-test="file-node-README.md"]').trigger('click');
    await flushPromises();

    expect(rightSidebarBridge.openTab).toHaveBeenCalledWith('fileViewer', { path: 'README.md' });
  });
});

describe('ProjectFileTree row context menu', () => {
  beforeEach(() => {
    resetHarness();
  });

  /**
   * Right-click a row and return the menu model the ContextMenu received.
   * @param wrapper
   * @param path
   */
  async function openMenuFor(
    wrapper: Awaited<ReturnType<typeof mountTree>>,
    path: string
  ): Promise<Array<Record<string, unknown>>> {
    await wrapper.get(`[data-test="file-node-${path}"]`).trigger('contextmenu');
    await flushPromises();
    const menu = wrapper.findComponent({ name: 'ContextMenu' });
    expect(menuShots.length).toBe(1);
    return menu.props('model') as Array<Record<string, unknown>>;
  }

  it('offers the web-build entries for a file row and anchors the popup', async () => {
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'README.md');
    const labels = items.filter(item => !item.separator).map(item => item.label);

    expect(labels).toEqual(['打开', '打开方式', '复制绝对路径', '复制相对路径', '添加到聊天']);
    // The popup was shown by the row's own event.
    expect(menuShots[0]).toBeInstanceOf(Event);
  });

  it('copies the absolute path (root joined) and the relative path', async () => {
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'README.md');

    (items.find(item => item.label === '复制绝对路径')!.command as () => void)();
    (items.find(item => item.label === '复制相对路径')!.command as () => void)();
    await flushPromises();

    expect(clipboard.copyTextToClipboard).toHaveBeenNthCalledWith(1, '/proj/README.md');
    expect(clipboard.copyTextToClipboard).toHaveBeenNthCalledWith(2, 'README.md');
    expect(toasts.toastSuccess).toHaveBeenCalled();
  });

  it('asks the session composer for a mention from Add to chat', async () => {
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'README.md');

    (items.find(item => item.label === '添加到聊天')!.command as () => void)();
    await flushPromises();

    expect(bus.emit).toHaveBeenCalledWith('chat:file-mention', {
      sessionId: 'sid-1',
      payload: { type: 'file', path: 'README.md', name: 'README.md' }
    });
    // The composer chip is the feedback; no transient toast in the corner.
    expect(toasts.toastInfo).not.toHaveBeenCalled();
  });

  it('opens a directory row instead of the viewer (Open mirrors a click)', async () => {
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'src');

    (items[0]!.command as () => void)();
    await flushPromises();

    expect(bridge.fetchProjectTree).toHaveBeenLastCalledWith('sid-1', 'src');
    expect(rightSidebarBridge.openTab).not.toHaveBeenCalled();
  });

  it('shows reveal and the system-app entry on the desktop build', async () => {
    desktop.desktopPlatform.mockReturnValue('windows');
    desktop.canUseDesktopFileActions.mockReturnValue(true);
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'README.md');
    const labels = items.filter(item => !item.separator).map(item => item.label);
    expect(labels).toContain('在资源管理器中显示');

    (items.find(item => item.label === '在资源管理器中显示')!.command as () => void)();
    await flushPromises();
    expect(desktop.revealPathInFileManager).toHaveBeenCalledWith('/proj/README.md');
    expect(toasts.toastError).not.toHaveBeenCalled();
  });

  it('reports a refused reveal instead of failing silently', async () => {
    desktop.canUseDesktopFileActions.mockReturnValue(true);
    desktop.revealPathInFileManager.mockResolvedValue(false);
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'README.md');

    (items.find(item => item.label === '在文件管理器中显示')!.command as () => void)();
    await flushPromises();

    expect(toasts.toastError).toHaveBeenCalled();
  });

  it('opens an HTML file in the browser panel as a blob URL', async () => {
    bridge.fetchProjectTree.mockResolvedValue({
      sessionId: 'sid-1',
      root: '/proj',
      path: '',
      entries: [{ name: 'index.html', type: 'file', size: 9 }],
      truncated: false,
      total: 1
    });
    bridge.fetchProjectFile.mockResolvedValue({ content: '<html></html>', size: 13 });
    const wrapper = await mountTree();
    const items = await openMenuFor(wrapper, 'index.html');

    (items.find(item => item.label === '用内置浏览器打开')!.command as () => void)();
    await flushPromises();

    expect(bridge.fetchProjectFile).toHaveBeenCalledWith('sid-1', 'index.html');
    expect(rightSidebarBridge.openTab).toHaveBeenCalledTimes(1);
    const [kind, payload] = rightSidebarBridge.openTab.mock.calls[0]!;
    expect(kind).toBe('browser');
    expect(String(payload.url)).toMatch(/^blob:/);
  });
});
