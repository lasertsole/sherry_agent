import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import DocxViewer from '../DocxViewer.vue';

/**
 * Light integration: mammoth is faked at its browser-bundle import seam and
 * the sanitizer at the directive, so these tests pin the handoff — bytes in,
 * converted HTML rendered through the `v-safe-html.fragment` channel, and the
 * failure line when the conversion rejects.
 */
const mammoth = vi.hoisted(() => ({ convertToHtml: vi.fn() }));

vi.mock('mammoth/mammoth.browser.js', () => ({ default: { convertToHtml: mammoth.convertToHtml } }));

const directiveStubs = {
  // The panel-level suites stub it the same way; the fragment path is pinned
  // by safeHtml.test.ts with the REAL directive.
  'safe-html': {
    mounted(el: HTMLElement, binding: { value: string }) {
      el.innerHTML = binding.value;
    },
    updated(el: HTMLElement, binding: { value: string }) {
      el.innerHTML = binding.value;
    }
  }
};

async function mountViewer() {
  const wrapper = mount(DocxViewer, { props: { bytes: new ArrayBuffer(16) }, global: { directives: directiveStubs } });
  await flushPromises();
  await flushPromises();
  return wrapper;
}

describe('DocxViewer', () => {
  beforeEach(() => {
    mammoth.convertToHtml.mockReset();
  });

  it('renders the converted HTML through the fragment directive', async () => {
    mammoth.convertToHtml.mockResolvedValue({ value: '<p><strong>hi</strong></p>', messages: [] });

    const wrapper = await mountViewer();

    expect(wrapper.get('[data-test="docx-viewer"]').html()).toContain('<strong>hi</strong>');
    expect(mammoth.convertToHtml).toHaveBeenCalledWith({ arrayBuffer: expect.any(ArrayBuffer) });
  });

  it('shows the failure line when the conversion rejects (F4)', async () => {
    mammoth.convertToHtml.mockRejectedValue(new Error('not an OOXML zip'));

    const wrapper = await mountViewer();

    expect(wrapper.get('[data-test="docx-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="docx-viewer"]').exists()).toBe(false);
  });

  it('re-converts when the bytes change', async () => {
    mammoth.convertToHtml.mockResolvedValue({ value: '<p>one</p>', messages: [] });
    const wrapper = mount(DocxViewer, { props: { bytes: new ArrayBuffer(2) }, global: { directives: directiveStubs } });
    await flushPromises();

    await wrapper.setProps({ bytes: new ArrayBuffer(4) });
    await flushPromises();

    expect(mammoth.convertToHtml).toHaveBeenCalledTimes(2);
  });
});
