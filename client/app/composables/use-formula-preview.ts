import { ref } from 'vue';

/**
 * Formula preview state (the chat's KaTeX click-to-zoom).
 *
 * A module-level singleton like `useImagePreview`: the chat bubble and the
 * overlay live in different component trees, and the overlay is mounted once
 * in `app.vue`.
 */
const previewHtml = ref('');
const isFormulaPreviewVisible = ref(false);

export function useFormulaPreview() {
  /**
   * Show a sanitized KaTeX fragment at full size.
   * @param html The formula's outer HTML (already rendered by v-safe-html).
   */
  const openFormulaPreview = (html: string) => {
    if (!html) return;
    previewHtml.value = html;
    isFormulaPreviewVisible.value = true;
  };

  const closeFormulaPreview = () => {
    isFormulaPreviewVisible.value = false;
  };

  return { previewHtml, isFormulaPreviewVisible, openFormulaPreview, closeFormulaPreview };
}
