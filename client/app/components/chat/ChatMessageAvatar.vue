<template>
  <!-- The box always occupies its 40px so bubbles keep their x-position; when the avatar
       is hidden (consecutive same-role message / tool row) the whole slot is fully
       transparent instead of showing the empty placeholder disc. -->
  <div
    class="flex justify-center items-center w-10 h-10 rounded-full overflow-hidden shrink-0"
    :class="hidden ? 'opacity-0' : 'bg-gray-100 dark:bg-gray-800'"
    :aria-hidden="hidden ? 'true' : undefined">
    <!-- An empty src means "this role has no avatar of its own" (the role tab's reset
         clears it, 编程助手 ships none): the neutral gray silhouette renders instead of
         a bare glyph, matching the placeholder the role tab shows. -->
    <img
      :class="['w-full h-full object-cover', { hidden }]"
      :src="src || DEFAULT_PLACEHOLDER_AVATAR"
      :alt="alt"
      loading="lazy"
      decoding="async" />
  </div>
</template>

<script setup lang="ts">
// DEFAULT_PLACEHOLDER_AVATAR is auto-imported from ~/composables/defaultCharacter.
interface Props {
  /** Avatar URL; when empty the neutral placeholder silhouette is rendered */
  src: string;
  /** Alt text for the avatar image */
  alt: string;
  /** Hide the avatar (consecutive same-role message or a tool row) */
  hidden: boolean;
}
defineProps<Props>();
</script>
