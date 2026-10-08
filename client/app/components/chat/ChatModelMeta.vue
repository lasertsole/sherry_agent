<template>
  <!-- Model metadata (model name + token usage). The token phrase is the
       affordance that opens the context viewer: hovering it turns the numbers
       blue (the app's accent), clicking opens the session's context tab — and
       several bubbles all lead to that same tab. -->
  <div class="mt-1 text-xs text-[#9CA3AF] dark:text-[#6B7280]">
    <template v-if="modelName">{{ modelName }}</template>
    <template v-if="inputTokens !== undefined || outputTokens !== undefined">
      <template v-if="modelName"> · </template>
      <button
        type="button"
        class="cursor-pointer rounded transition-colors hover:text-[#2563EB] dark:hover:text-[#60A5FA]"
        :title="title"
        :aria-label="title"
        data-test="model-meta-open-context"
        @click="emit('open-context')">
        {{ text }}
      </button>
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
  /** Hover tooltip of the clickable token phrase (already translated). */
  title?: string;
}
withDefaults(defineProps<Props>(), { title: '' });

const emit = defineEmits<{ 'open-context': [] }>();
</script>
