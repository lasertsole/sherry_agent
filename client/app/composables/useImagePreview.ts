import { computed, ref } from 'vue';

/** The gallery's image list; one entry is still a gallery of one. */
const galleryItems = ref<string[]>([]);
/** Index of the image currently shown. */
const currentIndex = ref(0);
/** Whether the lightbox is open. */
const isPreviewVisible = ref(false);

/** The image the overlay renders (empty when the gallery is closed). */
const previewSrc = computed(() => galleryItems.value[currentIndex.value] ?? '');

/**
 * Open the lightbox on a list of images.
 *
 * Empty entries are dropped and the start index is clamped, so a caller that
 * collected images from the DOM can never open an out-of-range position.
 * @param items Image sources, in display order
 * @param startIndex Index to show first
 */
export function openGallery(items: string[], startIndex = 0): void {
  const clean = items.filter(Boolean);
  if (clean.length === 0) return;
  galleryItems.value = clean;
  currentIndex.value = Math.min(Math.max(0, startIndex), clean.length - 1);
  isPreviewVisible.value = true;
}

/**
 * Open a single image (backward-compatible with the pre-gallery consumers).
 * @param src Image source
 */
export function openPreview(src: string): void {
  if (!src) return;
  openGallery([src], 0);
}

/** Close the lightbox and forget the gallery. */
export function closePreview(): void {
  isPreviewVisible.value = false;
  galleryItems.value = [];
  currentIndex.value = 0;
}

/** Show the next image, wrapping at the end (no-op for a single image). */
export function nextImage(): void {
  if (galleryItems.value.length < 2) return;
  currentIndex.value = (currentIndex.value + 1) % galleryItems.value.length;
}

/** Show the previous image, wrapping at the start (no-op for a single image). */
export function prevImage(): void {
  if (galleryItems.value.length < 2) return;
  currentIndex.value = (currentIndex.value - 1 + galleryItems.value.length) % galleryItems.value.length;
}

export function useImagePreview() {
  return {
    previewSrc,
    galleryItems,
    currentIndex,
    isPreviewVisible,
    openPreview,
    openGallery,
    closePreview,
    nextImage,
    prevImage
  };
}
