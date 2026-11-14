<template>
  <!-- Artifact images produced by a tool (a generated picture, a browser
       screenshot): thumbnails that open the shared gallery/lightbox. Rendered
       by the tool card and (compact) by the collapsed turn-history header. -->
  <div
    v-if="images.length"
    :class="variant === 'peek' ? 'flex shrink-0 items-center gap-1' : 'grid grid-cols-2 gap-2 sm:grid-cols-3'"
    data-test="artifact-image-gallery">
    <button
      v-for="(image, index) in visible"
      :key="image.uri"
      type="button"
      :class="
        variant === 'peek'
          ? 'h-6 w-6 shrink-0 overflow-hidden rounded border border-solid border-gray-200 dark:border-gray-600'
          : 'group relative aspect-square overflow-hidden rounded-md border border-solid border-gray-200 bg-white dark:border-gray-600 dark:bg-gray-900'
      "
      :aria-label="image.uri"
      :title="image.uri"
      data-test="artifact-image-thumb"
      @click.stop="open(index)">
      <img
        :src="image.url"
        alt=""
        loading="lazy"
        :class="
          variant === 'peek'
            ? 'h-full w-full object-cover'
            : 'h-full w-full object-cover transition-transform duration-200 group-hover:scale-105'
        " />
    </button>
    <span
      v-if="variant === 'peek' && images.length > visible.length"
      class="shrink-0 text-[10px] tabular-nums text-gray-400 dark:text-gray-500">
      +{{ images.length - visible.length }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ResolvedArtifactImage } from '~/composables/media-resolver';

interface Props {
  /** Free text to scan for `sherry-artifact://` references (a tool result). */
  text?: string | null;
  /** Already-extracted references; wins over ``text`` when non-empty. */
  uris?: string[];
  /** Grid of thumbnails, or the folded-turn header's compact peek row. */
  variant?: 'grid' | 'peek';
  /** How many thumbnails the peek row shows before the `+N` badge. */
  limit?: number;
}

const props = withDefaults(defineProps<Props>(), {
  text: '',
  uris: () => [],
  variant: 'grid',
  limit: 4
});

/** The images this gallery renders (deduplicated, unresolvable refs dropped). */
const images = computed<ResolvedArtifactImage[]>(() => {
  if (props.uris.length === 0) return resolveArtifactImages(props.text);
  const seen = new Set<string>();
  return props.uris.flatMap(uri => {
    const url = uri.startsWith(ARTIFACT_URI_PREFIX) ? artifactUriToHttp(uri) : null;
    if (!url || seen.has(uri)) return [];
    seen.add(uri);
    return [{ uri, url }];
  });
});

/** The subset the peek row shows (the grid shows every image). */
const visible = computed(() =>
  props.variant === 'peek' ? images.value.slice(0, Math.max(1, props.limit)) : images.value
);

/**
 * Open the shared lightbox on the clicked image (the whole gallery becomes the
 * swipe list, so arrows move across every generated image).
 * @param index Position of the clicked image in the gallery
 */
function open(index: number): void {
  openGallery(
    images.value.map(item => item.url),
    index
  );
}
</script>
