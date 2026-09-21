<template>
  <!-- Model metadata (model name + token usage) -->
  <div class="mt-1 text-xs text-[#9CA3AF] dark:text-[#6B7280]">
    <template v-if="modelName">{{ modelName }}</template>
    <template v-if="inputTokens !== undefined || outputTokens !== undefined">
      <template v-if="modelName"> · </template>
      {{ text }}
    </template>
  </div>
</template>

<script setup lang="ts">
interface Props {
  /** Model name (only present for AI messages that carry it) */
  modelName?: string;
  /** Input token count (undefined hides the token part) */
  inputTokens?: number;
  /** Output token count (undefined hides the token part) */
  outputTokens?: number;
  /** Pre-translated model metadata text (rendered only when token counts exist) */
  text: string;
}
defineProps<Props>();
</script>
