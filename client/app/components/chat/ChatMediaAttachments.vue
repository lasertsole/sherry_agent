<template>
  <template v-if="messageImages(message).length">
    <div class="flex flex-wrap gap-2 mt-2">
      <template
        v-for="(src, i) in messageImages(message)"
        :key="i">
        <!-- Media referenced by historical messages may no longer exist on disk (rows
             written before the media feature landed); on load failure, hide the broken
             image and show a placeholder block instead of a broken-image icon. -->
        <img
          v-if="!failedSources.has(resolveImageSrc(message, src))"
          :src="resolveImageSrc(message, src)"
          class="w-24 h-24 object-cover rounded-lg border border-solid border-gray-200 cursor-pointer hover:opacity-80 transition-opacity duration-200"
          role="button"
          tabindex="0"
          :aria-label="previewLabel"
          loading="lazy"
          decoding="async"
          @click="openPreview(resolveImageSrc(message, src))"
          @keydown.enter.prevent="openPreview(resolveImageSrc(message, src))"
          @keydown.space.prevent="openPreview(resolveImageSrc(message, src))"
          @error="imageErrorHandler($event, resolveImageSrc(message, src))" />
        <div
          v-else
          class="w-24 h-24 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 flex items-center justify-center text-xs text-gray-400 dark:text-gray-500">
          {{ loadFailedLabel }}
        </div>
      </template>
    </div>
  </template>
  <!-- Audio attachments -->
  <template v-if="messageAudios(message).length">
    <div class="flex flex-col gap-2 mt-2 min-w-[200px] max-w-full">
      <audio
        v-for="(src, i) in messageAudios(message)"
        :key="i"
        :src="resolveAudioSrc(message, src)"
        controls
        preload="none"
        class="w-full max-w-[280px] rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60" />
    </div>
  </template>
  <!-- Video attachments -->
  <template v-if="messageVideos(message).length">
    <div class="flex flex-col gap-2 mt-2 max-w-full">
      <video
        v-for="(src, i) in messageVideos(message)"
        :key="i"
        :src="resolveVideoSrc(message, src)"
        controls
        preload="none"
        class="max-w-[280px] max-h-56 rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-black" />
    </div>
  </template>
</template>

<script setup lang="ts">
import type { MessageItem } from '~/pages/home/type';

const { openPreview } = useImagePreview();

interface Props {
  /** Message whose images/audios/videos are rendered */
  message: MessageItem;
  /** Shared set of image srcs that already failed to load (broken media is hidden) */
  failedSources: Set<string>;
  /** Localized accessible label for the image preview action */
  previewLabel: string;
  /** Localized "image load failed" placeholder label */
  loadFailedLabel: string;
  /** Records an <img> load failure into the shared failed-source set */
  imageErrorHandler: (event: Event, src: string) => void;
}
defineProps<Props>();
</script>
