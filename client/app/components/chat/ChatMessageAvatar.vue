<template>
  <!-- The box always occupies its 40px so bubbles keep their x-position; when the avatar
       is hidden (consecutive same-role message / tool row) the whole slot is fully
       transparent instead of showing the empty placeholder disc. -->
  <div
    class="flex justify-center items-center w-10 h-10 rounded-full overflow-hidden shrink-0"
    :class="hidden ? 'opacity-0' : 'bg-gray-100 dark:bg-gray-800'"
    :aria-hidden="hidden ? 'true' : undefined">
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
