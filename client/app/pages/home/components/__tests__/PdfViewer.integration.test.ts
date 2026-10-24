import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PdfViewer from '../PdfViewer.vue';

/**
 * Light integration: the pdf.js module is faked at its import seam (the
 * bundled worker `?url` import resolves against the real installed package),
 * so these tests pin the TOOLBAR contract — page state, flip boundaries, the
 * 200-page truncation notice — not canvas pixels (happy-dom's 2D context is
 * inert by design).
 */
const pdfjs = vi.hoisted(() => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: vi.fn()
}));

vi.mock('pdfjs-dist', () => pdfjs);
// The bundled worker resolves through Vite's ?url pipeline; pin it so the
// load path never crosses a macrotask (real fs load of the 1 MB worker).
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/pdf.worker.min.mjs' }));

function fakePage() {
  return {
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    render: () => ({ cancel: vi.fn(), promise: Promise.resolve() })
  };
}

function fakeDoc(numPages: number) {
  return {
    numPages,
    getPage: async () => fakePage()
  };
}

async function mountViewer(numPages: number) {
  pdfjs.getDocument.mockReset();
  const doc = fakeDoc(numPages);
  // pdf.js v6 puts destroy() on the LOADING TASK, not the document proxy.
  const loadingTask = { promise: Promise.resolve(doc), destroy: vi.fn(async () => undefined) };
  pdfjs.getDocument.mockReturnValue(loadingTask);
  const wrapper = mount(PdfViewer, {
    props: { bytes: new ArrayBuffer(16) },
    global: { stubs: { Button: { template: '<button />' } } }
  });
  await flushPromises();
  await flushPromises();
  return { wrapper, loadingTask };
}

describe('PdfViewer', () => {
  beforeEach(() => {
    pdfjs.GlobalWorkerOptions.workerSrc = '';
  });

  it('loads the document and shows the 1/N page label', async () => {
    const { wrapper } = await mountViewer(3);

    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('1');
    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('3');
    expect(wrapper.find('[data-test="pdf-prev"]').attributes('disabled')).toBeDefined();
    expect(wrapper.find('[data-test="pdf-next"]').attributes('disabled')).toBeUndefined();
    // The worker URL was configured exactly once from the bundled asset.
    expect(pdfjs.GlobalWorkerOptions.workerSrc).toBeTruthy();
  });

  it('flips pages within bounds', async () => {
    const { wrapper } = await mountViewer(3);

    await wrapper.get('[data-test="pdf-next"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('2');

    await wrapper.get('[data-test="pdf-prev"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('1');
  });

  it('stops at the last page', async () => {
    const { wrapper } = await mountViewer(2);

    await wrapper.get('[data-test="pdf-next"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="pdf-next"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('2');
    expect(wrapper.find('[data-test="pdf-next"]').attributes('disabled')).toBeDefined();
  });

  it('truncates a huge document with a notice (F3)', async () => {
    const { wrapper } = await mountViewer(250);

    expect(wrapper.get('[data-test="pdf-truncated"]').exists()).toBe(true);
    // The toolbar clamps to the cap: paging stops at 200 even though the
    // document holds 250.
    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('200');
    for (let i = 0; i < 210; i++) {
      await wrapper.get('[data-test="pdf-next"]').trigger('click');
    }
    await flushPromises();
    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('200');
  });

  it('destroys the loading task on unmount', async () => {
    const { wrapper, loadingTask } = await mountViewer(3);

    wrapper.unmount();
    await flushPromises();

    expect(loadingTask.destroy).toHaveBeenCalled();
  });

  it('survives a load failure with an empty page count (F4)', async () => {
    pdfjs.getDocument.mockReturnValue({ promise: Promise.reject(new Error('bad pdf')) });

    const wrapper = mount(PdfViewer, {
      props: { bytes: new ArrayBuffer(4) },
      global: { stubs: { Button: { template: '<button />' } } }
    });
    await flushPromises();
    await flushPromises();

    expect(wrapper.get('[data-test="pdf-page-label"]').text()).toContain('0');
    expect(wrapper.find('[data-test="pdf-next"]').attributes('disabled')).toBeDefined();
  });
});
