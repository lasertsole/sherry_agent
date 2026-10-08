const previewSrc = ref<string>('');
const isPreviewVisible = ref(false);

export function useImagePreview() {
  const openPreview = (src: string) => {
    if (!src) return;
    previewSrc.value = src;
    isPreviewVisible.value = true;
  };

  const closePreview = () => {
    isPreviewVisible.value = false;
  };

  return { previewSrc, isPreviewVisible, openPreview, closePreview };
}
