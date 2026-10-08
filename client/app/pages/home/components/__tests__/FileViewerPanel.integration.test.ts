/**
 * The file viewer panel: markdown through the sanitizing directive, everything
 * else as plain text, refusals in place, and the store cache so that switching
 * tabs away and back does not re-fetch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import FileViewerPanel from '@/pages/home/components/FileViewerPanel.vue';
import { useFileViewerStore } from '@/stores/file-viewer';

const bridge = vi.hoisted(() => ({ fetchProjectFile: vi.fn() }));
vi.mock('~/composables/bridge', () => bridge);

const route = vi.hoisted(() => ({ params: { sid: 'sid-1' } }));

const stubs = {
  ProgressSpinner: { template: '<div class="spinner" />' }
};

async function mountPanel(path?: string) {
  const wrapper = mount(FileViewerPanel, {
    props: path === undefined ? {} : { payload: { path } },
    global: {
      stubs,
      directives: {
        // The real directive sanitizes + assigns innerHTML; the test only needs
        // the rendered text to prove the markdown branch was taken.
        'safe-html': {
          mounted(el: HTMLElement, binding: { value: string }) {
            el.innerHTML = binding.value;
          }
        }
      }
    }
  });
  await flushPromises();
  return wrapper;
}

describe('FileViewerPanel', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchProjectFile.mockReset();
    bridge.fetchProjectFile.mockResolvedValue({ path: 'README.md', content: '# Hi', size: 4 });
    vi.stubGlobal('useRoute', () => route);
    // Drive the REAL cache store (setup.ts's stub serves page-mount suites).
    vi.stubGlobal('useFileViewerStore', () => useFileViewerStore());
    vi.stubGlobal('useProjectDirectoryStore', () => ({
      stateFor: () => ({ directory: '/proj', pendingDirectory: null, error: null }),
      clearError: () => {}
    }));
  });

  it('renders markdown through the sanitizing directive', async () => {
    const wrapper = await mountPanel('README.md');

    const md = wrapper.get('[data-test="file-viewer-md"]');
    expect(md.html()).toContain('# Hi');
    expect(wrapper.find('[data-test="file-viewer-pre"]').exists()).toBe(false);
  });

  it('renders other text in a <pre>', async () => {
    bridge.fetchProjectFile.mockResolvedValue({ path: 'main.py', content: 'print(1)', size: 8 });

    const wrapper = await mountPanel('main.py');

    expect(wrapper.get('[data-test="file-viewer-pre"]').text()).toContain('print(1)');
    expect(wrapper.find('[data-test="file-viewer-md"]').exists()).toBe(false);
  });

  it('caches the content: a remount does not fetch again', async () => {
    await mountPanel('README.md');
    expect(bridge.fetchProjectFile).toHaveBeenCalledTimes(1);

    const second = await mountPanel('README.md');

    expect(bridge.fetchProjectFile).toHaveBeenCalledTimes(1);
    expect(second.get('[data-test="file-viewer-md"]').html()).toContain('# Hi');
  });

  it('renders a refusal in place', async () => {
    bridge.fetchProjectFile.mockRejectedValue(new Error('binary file (not UTF-8 text)'));

    const wrapper = await mountPanel('data.bin');

    expect(wrapper.get('[data-test="file-viewer-error"]').text()).toContain('binary');
  });

  it('exposes a cache the directory switch can drop', async () => {
    const store = useFileViewerStore();
    store.put('sid-1', 'README.md', '# Hi', 4);
    expect(store.get('sid-1', 'README.md')?.content).toBe('# Hi');

    store.clear();

    expect(store.get('sid-1', 'README.md')).toBeNull();
  });

  it('asks for a file when the tab has no payload', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.get('[data-test="file-viewer-error"]').text()).toContain('未选择文件');
  });
});
