import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';

// `useImagePreview.ts` uses `ref` as a Nuxt auto-import (no explicit import in
// source), which is not available in a bare happy-dom environment. We stub the
// real `vue` ref/computed onto globalThis in the top level, THEN dynamically
// import the module under test inside beforeAll — ES module imports are hoisted,
// so a static `import { useImagePreview }` would evaluate the module body before
// the stub assignment runs. Dynamic import guarantees the stubs are in place.

import { ref, computed } from 'vue';

vi.stubGlobal('ref', ref);
vi.stubGlobal('computed', computed);

type ImagePreview = {
  previewSrc: { value: string };
  isPreviewVisible: { value: boolean };
  openPreview: (src: string) => void;
  closePreview: () => void;
};

let useImagePreview: () => ImagePreview;

beforeAll(async () => {
  const mod = await import('../useImagePreview');
  useImagePreview = mod.useImagePreview as () => ImagePreview;
});

// Module-level singleton: the three refs are shared across all calls.
describe('useImagePreview', () => {
  beforeEach(() => {
    // Reset singleton state between tests so `previewSrc`/`isPreviewVisible`
    // don't leak from one case to the next. `closePreview()` only flips
    // `isPreviewVisible` (it does NOT clear `previewSrc`), so we must also
    // clear the src ref manually.
    const api = useImagePreview();
    api.previewSrc.value = '';
    api.closePreview();
  });

  it('starts hidden with an empty preview source', () => {
    const { previewSrc, isPreviewVisible } = useImagePreview();
    expect(previewSrc.value).toBe('');
    expect(isPreviewVisible.value).toBe(false);
  });

  it('openPreview shows a valid src and closes hides it again', () => {
    const { openPreview, previewSrc, isPreviewVisible, closePreview } = useImagePreview();

    openPreview('data:image/png;base64,AAAA');
    expect(previewSrc.value).toBe('data:image/png;base64,AAAA');
    expect(isPreviewVisible.value).toBe(true);

    closePreview();
    expect(isPreviewVisible.value).toBe(false);
  });

  it('openPreview ignores an empty src but accepts whitespace', () => {
    const { openPreview, previewSrc, isPreviewVisible } = useImagePreview();

    openPreview('');
    expect(previewSrc.value).toBe('');
    expect(isPreviewVisible.value).toBe(false);

    // NB: source checks `if (!src) return;` — whitespace is truthy, so it
    // IS applied (no trimming).
    openPreview('   ');
    expect(previewSrc.value).toBe('   ');
    expect(isPreviewVisible.value).toBe(true);
  });

  it('shares the same singleton state across multiple invocations', () => {
    const a = useImagePreview();
    const b = useImagePreview();

    a.openPreview('/avatar/user.jpg');
    expect(b.previewSrc.value).toBe('/avatar/user.jpg');
    expect(b.isPreviewVisible.value).toBe(true);

    b.closePreview();
    expect(a.isPreviewVisible.value).toBe(false);
  });
});
