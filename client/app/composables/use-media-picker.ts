import { ref, useTemplateRef } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';

/** Media kind selectable in the chat toolbar (`useMediaPicker(kind)`). */
export type MediaPickerKind = 'image' | 'audio' | 'video';

/** One selected (pending-send) media item: pure base64 payload plus its file name. */
export interface MediaPick {
  base64: string;
  name: string;
}

interface MediaPickerConfig {
  /** Maximum number of items allowed per message. */
  max: number;
  /** i18n key for the "at the limit" alert. */
  maxKey: string;
  /** i18n key for the "selection truncated" alert. */
  maxExceedKey: string;
  /** MIME prefix used both for the file-type check and the log label. */
  mimePrefix: string;
  /** Log label of the selection callback. */
  logLabel: string;
  /** Noun used in the read-failure log message. */
  logNoun: string;
}

const PICKER_CONFIG: Record<MediaPickerKind, MediaPickerConfig> = {
  image: {
    max: 10,
    maxKey: 'chatInput.maxImages',
    maxExceedKey: 'chatInput.maxImagesExceed',
    mimePrefix: 'image/',
    logLabel: 'onImageSelected',
    logNoun: '图片'
  },
  audio: {
    max: 5,
    maxKey: 'chatInput.maxAudios',
    maxExceedKey: 'chatInput.maxAudiosExceed',
    mimePrefix: 'audio/',
    logLabel: 'onAudioSelected',
    logNoun: '音频'
  },
  video: {
    max: 3,
    maxKey: 'chatInput.maxVideos',
    maxExceedKey: 'chatInput.maxVideosExceed',
    mimePrefix: 'video/',
    logLabel: 'onVideoSelected',
    logNoun: '视频'
  }
};

/**
 * Read a file as a DataURL (includes the `data:<mime>;base64` prefix; strip the prefix before sending).
 * @param file
 */
function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * Pending-media selection state for one toolbar media kind (image | audio | video).
 *
 * Backs the hidden `<input type="file">` declared by the page: the page keeps the
 * input elements in its template and binds `trigger` / `onSelected` / `remove` /
 * `selected` to them. `fileInputRef` resolves the input via the template ref named
 * `${kind}FileInputRef` (e.g. `imageFileInputRef`).
 *
 * @param kind Media kind; drives the per-message limit, the MIME check, the i18n
 *             alert keys and the template ref name
 * @returns `selected` (pending list), `fileInputRef` (hidden input), `trigger`
 *          (open the system picker), `onSelected` (input change handler), `remove` (drop one item)
 */
export function useMediaPicker(kind: MediaPickerKind) {
  const { t } = useI18n();
  const config = PICKER_CONFIG[kind];
  const selected = ref<MediaPick[]>([]);
  const fileInputRef = useTemplateRef<HTMLInputElement>(`${kind}FileInputRef`);

  /**
   * Open the system file picker for this kind (bound to the toolbar button).
   */
  const trigger = () => {
    fileInputRef.value?.click();
  };

  /**
   * Remove one selected item by index.
   * @param index
   */
  const remove = (index: number) => {
    selected.value.splice(index, 1);
    selected.value = [...selected.value];
  };

  /**
   * Selection callback: read the picked files as base64 and add them to the
   * pending-send list (capped at the kind's per-message limit).
   * @param event
   */
  const onSelected = async (event: Event) => {
    const input = event.target as HTMLInputElement;
    // Copy to a plain array snapshot before resetting input.value.
    // input.files is a "live" FileList — once value is emptied, the browser immediately clears that FileList;
    // reading files afterwards would yield an empty array, so selected would never be populated and the preview would not show.
    const files = Array.from(input.files ?? []);
    input.value = ''; // Allow re-selecting the same file
    if (files.length === 0) return;

    // Count limit: truncate the excess and notify the user
    const remaining = config.max - selected.value.length;
    if (remaining <= 0) {
      alert(t(config.maxKey, { count: config.max }));
      return;
    }
    const accepted = files.slice(0, remaining);
    if (files.length > remaining) {
      alert(t(config.maxExceedKey, { count: config.max, extra: files.length - remaining }));
    }

    for (const file of accepted) {
      if (!file.type.startsWith(config.mimePrefix)) continue;
      try {
        const dataUrl = await readFileAsDataUrl(file);
        // data:<mime>;base64,xxxxx -> keep only the base64 part
        const base64 = extractBase64(dataUrl);
        selected.value.push({ base64, name: file.name });
      } catch (e) {
        logUtil.w(`[${config.logLabel}] 读取${config.logNoun}失败：`, file.name, e);
      }
    }
    selected.value = [...selected.value];
  };

  return { selected, fileInputRef, trigger, onSelected, remove };
}
