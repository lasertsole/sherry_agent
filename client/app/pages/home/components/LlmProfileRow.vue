<template>
  <div
    role="button"
    tabindex="0"
    :class="[
      'flex items-center gap-2 rounded-md border px-2 py-1.5 text-xs cursor-pointer transition-colors',
      selected
        ? 'border-theme-main bg-blue-50 dark:bg-blue-900/20'
        : 'border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/40'
    ]"
    @click="emit('select')"
    @keydown.enter.prevent="emit('select')"
    @keydown.space.prevent="emit('select')">
    <span
      class="h-2 w-2 shrink-0 rounded-full"
      :class="active ? 'bg-emerald-500' : 'bg-transparent'"
      :title="active ? t('config.llm.applied') : ''"
      :data-active="active ? 'true' : 'false'"></span>
    <span class="min-w-0 flex-1 truncate">{{ entry.label }}</span>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

defineProps<{
  /** Row model: id + display label. */
  entry: { id: string; label: string };
  /** Whether this row is the one shown on the right (selected for viewing). */
  selected: boolean;
  /** Whether this row is the one applied to `.env` (green dot). */
  active: boolean;
}>();

const emit = defineEmits<{ (e: 'select'): void }>();
const { t } = useI18n();
</script>
