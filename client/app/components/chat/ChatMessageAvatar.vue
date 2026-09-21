<template>
  <div
    class="flex justify-center items-center w-10 h-10 rounded-full overflow-hidden shrink-0 bg-gray-100 dark:bg-gray-800">
    <!-- Avatar area: consecutive messages and tool calls all hide the avatar (hidden img,
         consistent with the consecutive-message rendering) -->
    <img
      v-if="src"
      :class="['w-full h-full object-cover', { hidden }]"
      :src="src"
      :alt="alt"
      loading="lazy"
      decoding="async" />
    <span
      v-else
      :class="['pi pi-user', { hidden }]"></span>
  </div>
</template>

<script setup lang="ts">
interface Props {
  /** Avatar URL; when empty the user icon fallback is rendered */
  src: string;
  /** Alt text for the avatar image */
  alt: string;
  /** Hide the avatar (consecutive same-role message or a tool row) */
  hidden: boolean;
}
defineProps<Props>();
</script>
