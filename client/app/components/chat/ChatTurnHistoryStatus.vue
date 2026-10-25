<template>
  <!-- Work summary trigger for a settled turn: the execution history (tool
       cards, intermediate text, reasoning) is collapsed behind this row while
       the final assistant answer stays below it. -->
  <button
    type="button"
    :class="[
      'flex w-fit max-w-full cursor-pointer select-none items-center gap-1.5 rounded-md border border-solid px-2.5 py-1 text-left text-xs font-medium transition-colors duration-200',
      expanded
        ? 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-600 dark:bg-gray-800/60 dark:text-gray-300'
        : 'border-gray-100 bg-gray-50/60 text-[#6B7280] hover:bg-gray-100/80 dark:border-gray-700/60 dark:bg-gray-800/30 dark:text-[#9CA3AF] dark:hover:bg-gray-800/60'
    ]"
    :aria-expanded="expanded"
    :title="expanded ? collapseLabel : expandLabel"
    :aria-label="expanded ? collapseLabel : expandLabel"
    data-test="turn-history-status"
    @click="emit('toggle')">
    <i
      class="pi pi-wrench text-[10px]"
      aria-hidden="true"></i>
    <span class="min-w-0 truncate">{{ label }}</span>
    <i
      :class="['pi pi-chevron-down text-[10px] transition-transform duration-200', { 'rotate-180': expanded }]"
      aria-hidden="true"></i>
  </button>
</template>

<script setup lang="ts">
interface Props {
  /** Pre-translated summary (tool count / thinking / execution + duration). */
  label: string;
  /** Whether the hidden execution history is open. */
  expanded: boolean;
  /** Accessible label when expanding. */
  expandLabel: string;
  /** Accessible label when collapsing. */
  collapseLabel: string;
}

defineProps<Props>();
const emit = defineEmits<{ toggle: [] }>();
</script>
