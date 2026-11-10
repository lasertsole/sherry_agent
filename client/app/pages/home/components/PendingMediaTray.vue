<template>
  <!-- Image preview area (kept separate above the input box, so it does not squeeze the h-40 input box pushing the send button up / clipping the ✕ button) -->
  <template v-if="images.length > 0">
    <div
      class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
      <div
        v-for="(img, idx) in images"
        :key="idx"
        class="relative shrink-0 group">
        <img
          :src="`data:image/*;base64,${img.base64}`"
          :alt="img.name"
          class="w-16 h-16 object-cover rounded-lg border border-solid border-gray-light dark:border-gray-dark cursor-pointer hover:opacity-80 transition-opacity duration-200"
          role="button"
          tabindex="0"
          :aria-label="t('a11y.previewImage')"
          @click="openPreview(`data:image/*;base64,${img.base64}`)"
          @keydown.enter.prevent="openPreview(`data:image/*;base64,${img.base64}`)"
          @keydown.space.prevent="openPreview(`data:image/*;base64,${img.base64}`)" />
        <button
          type="button"
          :title="t('chatBox.removeImage')"
          class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
          @click="emit('remove-image', idx)">
          ✕
        </button>
      </div>
    </div>
  </template>
  <!-- Audio preview area (kept separate above the input box, at the same level as the image preview area) -->
  <template v-if="audios.length > 0">
    <div
      class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
      <div
        v-for="(audio, idx) in audios"
        :key="idx"
        class="relative shrink-0 group">
        <div
          class="flex items-center gap-2 px-3 py-2 rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-gray-800">
          <span class="pi pi-volume-down text-xs text-[#6B7280]"></span>
          <span class="text-xs font-medium text-[#111827] dark:text-[#E5E7EB] max-w-32 truncate">{{ audio.name }}</span>
        </div>
        <button
          type="button"
          :title="t('chatBox.removeAudio')"
          class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
          @click="emit('remove-audio', idx)">
          ✕
        </button>
      </div>
    </div>
  </template>
  <!-- Video preview area (kept separate above the input box, at the same level as the image/audio preview areas) -->
  <template v-if="videos.length > 0">
    <div
      class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
      <div
        v-for="(video, idx) in videos"
        :key="idx"
        class="relative shrink-0 group">
        <video
          :src="`data:video/*;base64,${video.base64}`"
          class="w-32 h-20 object-cover rounded-lg border border-solid border-gray-light dark:border-gray-dark"
          muted
          playsinline
          preload="metadata" />
        <button
          type="button"
          :title="t('chatBox.removeVideo')"
          class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
          @click="emit('remove-video', idx)">
          ✕
        </button>
      </div>
    </div>
  </template>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';
import type { MediaPick } from '~/composables/use-media-picker';

/**
 * The composer's pending-attachment tray: image / audio / video previews with a
 * remove control on each. Purely presentational — the page owns the selections
 * (`usePendingMedia`) and removes by index.
 */
defineProps<{
  /** Pending images (raw base64 + file name). */
  images: MediaPick[];
  /** Pending audios. */
  audios: MediaPick[];
  /** Pending videos. */
  videos: MediaPick[];
}>();

const emit = defineEmits<{
  (e: 'remove-image', index: number): void;
  (e: 'remove-audio', index: number): void;
  (e: 'remove-video', index: number): void;
}>();

const { t } = useI18n();
const { openPreview } = useImagePreview();
</script>
