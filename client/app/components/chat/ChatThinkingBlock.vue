<template>
  <!-- Model thinking/reasoning block (collapsible) -->
  <div :class="['w-fit mb-1 text-sm transition-colors duration-200', { 'rounded-xl': consecutive }]">
    <button
      type="button"
      :class="[
        'flex items-center gap-2 w-full text-left cursor-pointer select-none px-3 py-1.5 text-xs font-medium rounded-md border border-solid transition-colors duration-200',
        expanded
          ? 'bg-gray-50 dark:bg-gray-800/60 border-gray-200 dark:border-gray-600 text-gray-500 dark:text-gray-400'
          : 'bg-gray-50/60 dark:bg-gray-800/30 border-gray-100 dark:border-gray-700/60 text-[#6B7280] dark:text-[#9CA3AF]'
      ]"
      @click="$emit('toggle')">
      <span class="pi pi-brain text-xs"></span>
      <span>{{ label }}</span>
      <span
        :class="[
          'pi pi-chevron-down text-xs ml-auto transition-transform duration-200',
          { 'rotate-180': expanded }
        ]"></span>
    </button>
    <div
      v-if="expanded"
      class="mt-1 px-3 py-2 text-xs whitespace-pre-wrap break-words leading-relaxed text-gray-500 dark:text-gray-400 border-l-2 border-solid border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/30 rounded-r-md">
      {{ reasoning }}
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  /** Model thinking/reasoning text (the parent only renders this when it is non-empty) */
  reasoning: string;
  /** Whether the collapsible body is expanded */
  expanded: boolean;
  /** Whether the previous visible message shares this message's role (rounded-corner variant) */
  consecutive: boolean;
  /** Localized "Thinking" label */
  label: string;
}
defineProps<Props>();
defineEmits<{ toggle: [] }>();
</script>
