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

const bridge = vi.hoisted(() => ({ fetchProjectTree: vi.fn() }));
vi.mock('~/composables/bridge', () => bridge);

const dirState = vi.hoisted(() => ({ directory: '/proj' as string | null }));

const rightSidebarBridge = vi.hoisted(() => ({ openTab: vi.fn() }));

const stubs = {
  // Functional recursive stub: renders each node as a button (self-referencing
  // so nested children appear) and emits the parent's toggle/open events.
  FileTreeNode: {
    name: 'FileTreeNode',
    props: ['node', 'depth', 'sessionId', 'loadingPaths'],
    emits: ['toggle', 'open'],
    template: `
      <div>
        <button
          class="node"
          :data-test="'file-node-' + node.path"
          @click="$emit(node.type === 'dir' ? 'toggle' : 'open', node)">{{ node.name }}</button>
        <FileTreeNode
          v-for="child in node.children"
          :key="child.path"
          :node="child"
          :depth="depth + 1"
          :session-id="sessionId"
          :loading-paths="loadingPaths"
          @toggle="n => $emit('toggle', n)"
          @open="n => $emit('open', n)" />
      </div>`
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

describe('ProjectFileTree', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
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
