import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';

// `use-formula-preview.ts` uses `ref` as a Nuxt auto-import in the app; in a bare
// Vitest environment the module body would run before that global exists, so
// stub it first and import the module dynamically (same pattern as
// useImagePreview.test.ts).
import { ref } from 'vue';

vi.stubGlobal('ref', ref);

type FormulaPreview = {
  previewHtml: { value: string };
  isFormulaPreviewVisible: { value: boolean };
  openFormulaPreview: (html: string) => void;
  closeFormulaPreview: () => void;
};

let useFormulaPreview: () => FormulaPreview;

beforeAll(async () => {
  const mod = await import('../use-formula-preview');
  useFormulaPreview = mod.useFormulaPreview as () => FormulaPreview;
});

describe('useFormulaPreview', () => {
  beforeEach(() => {
    const api = useFormulaPreview();
    api.previewHtml.value = '';
    api.closeFormulaPreview();
  });

  it('starts hidden with empty content', () => {
    const { previewHtml, isFormulaPreviewVisible } = useFormulaPreview();
    expect(previewHtml.value).toBe('');
    expect(isFormulaPreviewVisible.value).toBe(false);
  });

  it('opens with the sanitized formula fragment and closes again', () => {
    const { openFormulaPreview, previewHtml, isFormulaPreviewVisible, closeFormulaPreview } = useFormulaPreview();

    openFormulaPreview('<span class="katex">x</span>');
    expect(previewHtml.value).toContain('katex');
    expect(isFormulaPreviewVisible.value).toBe(true);

    closeFormulaPreview();
    expect(isFormulaPreviewVisible.value).toBe(false);
  });

  it('ignores empty content', () => {
    const { openFormulaPreview, isFormulaPreviewVisible } = useFormulaPreview();
    openFormulaPreview('');
    expect(isFormulaPreviewVisible.value).toBe(false);
  });

  it('shares singleton state across calls', () => {
    const a = useFormulaPreview();
    const b = useFormulaPreview();

    a.openFormulaPreview('<span class="katex">y</span>');
    expect(b.previewHtml.value).toContain('katex');
    expect(b.isFormulaPreviewVisible.value).toBe(true);
  });
});
