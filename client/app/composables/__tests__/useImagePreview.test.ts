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
  galleryItems: { value: string[] };
  currentIndex: { value: number };
  isPreviewVisible: { value: boolean };
  openPreview: (src: string) => void;
  openGallery: (items: string[], startIndex?: number) => void;
  closePreview: () => void;
  nextImage: () => void;
  prevImage: () => void;
};

let useImagePreview: () => ImagePreview;

beforeAll(async () => {
  const mod = await import('../useImagePreview');
  useImagePreview = mod.useImagePreview as () => ImagePreview;
});

// Module-level singleton: the gallery state is shared across all calls.
describe('useImagePreview', () => {
  beforeEach(() => {
    // closePreview resets visibility, the gallery list and the index.
    useImagePreview().closePreview();
  });

  it('starts hidden with an empty preview source', () => {
    const { previewSrc, isPreviewVisible, galleryItems } = useImagePreview();
    expect(previewSrc.value).toBe('');
    expect(isPreviewVisible.value).toBe(false);
    expect(galleryItems.value).toEqual([]);
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

  it('opens a gallery at the requested index and clamps it', () => {
    const { openGallery, currentIndex, previewSrc, isPreviewVisible } = useImagePreview();

    openGallery(['a', 'b', 'c'], 1);
    expect(isPreviewVisible.value).toBe(true);
    expect(currentIndex.value).toBe(1);
    expect(previewSrc.value).toBe('b');

    openGallery(['a', 'b'], 99);
    expect(currentIndex.value).toBe(1);
  });

  it('drops empty entries and refuses an empty gallery', () => {
    const { openGallery, galleryItems, isPreviewVisible } = useImagePreview();

    openGallery(['', 'a'], 0);
    expect(galleryItems.value).toEqual(['a']);

    useImagePreview().closePreview();
    openGallery([], 0);
    expect(isPreviewVisible.value).toBe(false);
  });

  it('wraps next/prev and leaves a single image fixed', () => {
    const { openGallery, openPreview, nextImage, prevImage, currentIndex } = useImagePreview();

    openGallery(['a', 'b', 'c'], 2);
    nextImage();
    expect(currentIndex.value).toBe(0);
    prevImage();
    expect(currentIndex.value).toBe(2);

    openPreview('only');
    nextImage();
    prevImage();
    expect(currentIndex.value).toBe(0);
  });

  it('closePreview resets the gallery', () => {
    const { openGallery, closePreview, galleryItems, currentIndex, previewSrc, isPreviewVisible } = useImagePreview();

    openGallery(['a', 'b'], 1);
    closePreview();
    expect(isPreviewVisible.value).toBe(false);
    expect(galleryItems.value).toEqual([]);
    expect(currentIndex.value).toBe(0);
    expect(previewSrc.value).toBe('');
  });
});
