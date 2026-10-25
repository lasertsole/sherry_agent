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

const bridge = vi.hoisted(() => ({ fetchProjectFile: vi.fn(), fetchProjectFileBlob: vi.fn() }));
vi.mock('~/composables/bridge', () => bridge);

// The image branch's click-to-zoom goes through the global overlay composable;
// mock it at the module (unimport resolves the bare symbol to the real module,
// shadowing setup.ts's globalThis stub).
const imagePreview = vi.hoisted(() => ({ openPreview: vi.fn() }));
vi.mock('~/composables/useImagePreview', () => ({ useImagePreview: () => imagePreview }));

const route = vi.hoisted(() => ({ params: { sid: 'sid-1' } }));

const stubs = {
  ProgressSpinner: { template: '<div class="spinner" />' },
  // The real viewers pull pdf.js / mammoth; the panel-level tests only need to
  // prove the BRANCH was taken and the bytes were handed over.
  PdfViewer: { template: '<div data-test="pdf-stub" />', props: ['bytes'] },
  DocxViewer: { template: '<div data-test="docx-stub" />', props: ['bytes'] }
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
    bridge.fetchProjectFileBlob.mockReset();
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

describe('FileViewerPanel — binary preview branches', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchProjectFile.mockReset();
    bridge.fetchProjectFileBlob.mockReset();
    vi.stubGlobal('useRoute', () => route);
    vi.stubGlobal('useFileViewerStore', () => useFileViewerStore());
    vi.stubGlobal('useProjectDirectoryStore', () => ({
      stateFor: () => ({ directory: '/proj', pendingDirectory: null, error: null }),
      clearError: () => {}
    }));
  });

  it('routes a PDF through the blob endpoint into PdfViewer', async () => {
    const bytes = new ArrayBuffer(8);
    bridge.fetchProjectFileBlob.mockResolvedValue({ bytes, mime: 'application/pdf' });

    const wrapper = await mountPanel('doc.pdf');

    expect(wrapper.find('[data-test="file-viewer-pdf"]').exists()).toBe(true);
    expect(bridge.fetchProjectFileBlob).toHaveBeenCalledTimes(1);
    expect(bridge.fetchProjectFile).not.toHaveBeenCalled();
    // The cached blob entry is what the second mount renders from.
    await mountPanel('doc.pdf');
    expect(bridge.fetchProjectFileBlob).toHaveBeenCalledTimes(1);
  });

  it('routes a docx into DocViewer', async () => {
    bridge.fetchProjectFileBlob.mockResolvedValue({
      bytes: new ArrayBuffer(4),
      mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    });

    const wrapper = await mountPanel('report.docx');

    expect(wrapper.find('[data-test="file-viewer-docx"]').exists()).toBe(true);
    expect(bridge.fetchProjectFile).not.toHaveBeenCalled();
  });

  it('renders an image via an object URL and opens the global overlay on click', async () => {
    bridge.fetchProjectFileBlob.mockResolvedValue({
      bytes: new TextEncoder().encode('png').buffer as ArrayBuffer,
      mime: 'image/png'
    });

    const wrapper = await mountPanel('logo.png');

    const img = wrapper.get('[data-test="file-viewer-image"] img');
    expect(img.attributes('src')).toMatch(/^blob:/);
    await img.trigger('click');
    expect(imagePreview.openPreview).toHaveBeenCalledWith(expect.stringMatching(/^blob:/));
  });

  it('falls back to the text endpoint for unknown suffixes (F4)', async () => {
    bridge.fetchProjectFile.mockResolvedValue({ path: 'weird.qqqzz', content: 'raw', size: 3 });

    const wrapper = await mountPanel('weird.qqqzz');

    expect(wrapper.get('[data-test="file-viewer-pre"]').text()).toContain('raw');
    expect(bridge.fetchProjectFileBlob).not.toHaveBeenCalled();
  });

  it('falls back to the text endpoint for legacy .doc (not OOXML)', async () => {
    bridge.fetchProjectFile.mockResolvedValue({ path: 'old.doc', content: 'legacy', size: 6 });

    const wrapper = await mountPanel('old.doc');

    expect(wrapper.get('[data-test="file-viewer-pre"]').text()).toContain('legacy');
    expect(bridge.fetchProjectFileBlob).not.toHaveBeenCalled();
  });

  it('shows a blob refusal in the error line', async () => {
    bridge.fetchProjectFileBlob.mockRejectedValue(new Error('file too large (99 bytes, limit 48)'));

    const wrapper = await mountPanel('big.pdf');

    expect(wrapper.get('[data-test="file-viewer-error"]').text()).toContain('too large');
  });
});
