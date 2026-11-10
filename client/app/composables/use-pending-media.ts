/**
 * Pending-attachment slice of the session composer: the three per-kind pickers
 * (image / audio / video), the payload snapshot the stream send reads, the
 * "taken by the send" clear, and the whiteboard's programmatic image append.
 *
 * The page keeps the hidden `<input type="file">` elements in its template and
 * binds `onSelected` / `trigger` / `remove` to them; this slice is only the
 * state.
 */
import { useI18n } from 'vue-i18n';
import type { MediaPick } from '~/composables/use-media-picker';

/** Images one message may carry (the whiteboard append uses the same cap). */
const MAX_PENDING_IMAGES = 10;

/**
 * Create the pending-attachment slice for the session composer.
 */
export function usePendingMedia() {
  const { t } = useI18n();

  const {
    selected: selectedImages,
    trigger: triggerImagePicker,
    onSelected: onImageSelected,
    remove: removeImage
  } = useMediaPicker('image');

  const {
    selected: selectedAudios,
    trigger: triggerAudioPicker,
    onSelected: onAudioSelected,
    remove: removeAudio
  } = useMediaPicker('audio');

  const {
    selected: selectedVideos,
    trigger: triggerVideoPicker,
    onSelected: onVideoSelected,
    remove: removeVideo
  } = useMediaPicker('video');

  /** Snapshot the pending media base64 payloads for the outgoing message. */
  const getPendingMedia = () => ({
    images: selectedImages.value.map(img => img.base64),
    audios: selectedAudios.value.map(a => a.base64),
    videos: selectedVideos.value.map(v => v.base64)
  });

  /** Clear the pending media selections (send took them). */
  const clearMediaSelection = () => {
    selectedImages.value = [];
    selectedAudios.value = [];
    selectedVideos.value = [];
  };

  /**
   * Append one programmatically produced image (the whiteboard's "add to
   * conversation" export) to the pending tray. The cap and its alert mirror
   * the image picker's.
   * @param base64 Raw base64 payload (no `data:` prefix).
   * @param name Display/attachment name.
   */
  const addPendingImage = (base64: string, name: string): void => {
    if (selectedImages.value.length >= MAX_PENDING_IMAGES) {
      toastWarn(t('chatInput.maxImagesExceed', { count: MAX_PENDING_IMAGES, extra: 1 }));
      return;
    }
    const image: MediaPick = { base64, name };
    selectedImages.value = [...selectedImages.value, image];
  };

  return {
    selectedImages,
    selectedAudios,
    selectedVideos,
    triggerImagePicker,
    onImageSelected,
    removeImage,
    triggerAudioPicker,
    onAudioSelected,
    removeAudio,
    triggerVideoPicker,
    onVideoSelected,
    removeVideo,
    getPendingMedia,
    clearMediaSelection,
    addPendingImage
  };
}
