<template>
  <div
    class="p-3 border border-solid rounded-lg bg-white dark:bg-[#2a2a36]/[0.6] md:hover:bg-[#e4efff] dark:md:hover:bg-[#1e2a3a] cursor-pointer transition-colors"
    :class="{ 'border-theme-main': active }"
    data-test="list-item-card"
    @click="$emit('click')">
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-2 min-w-0">
        <i
          v-if="icon"
          :class="icon"
          class="text-gray-400 shrink-0" />
        <span
          class="font-medium text-sm truncate"
          data-test="list-item-title"
          >{{ title }}</span
        >
      </div>
      <div
        v-if="badges?.length"
        class="flex gap-1 shrink-0">
        <span
          v-for="(b, index) in badges"
          :key="`${b.value}-${index}`"
          class="px-1.5 py-0.5 rounded text-xs"
          :class="badgeClass(b.severity)"
          data-test="list-item-badge"
          >{{ b.value }}</span
        >
      </div>
    </div>
    <div
      v-if="meta?.length"
      class="flex flex-wrap gap-3 mt-1 text-xs text-gray-400">
      <span
        v-for="(m, index) in meta"
        :key="`${m.value}-${index}`"
        data-test="list-item-meta">
        <i
          v-if="m.icon"
          :class="m.icon" />
        {{ m.value }}
      </span>
    </div>
    <slot name="extra" />
  </div>
</template>

<script setup lang="ts">
interface BadgeSpec {
  value: string;
  severity?: 'info' | 'success' | 'warn' | 'danger' | 'secondary';
}
interface MetaSpec {
  icon?: string;
  label?: string;
  value: string;
}
interface Props {
  icon?: string;
  title: string;
  active?: boolean;
  badges?: BadgeSpec[];
  meta?: MetaSpec[];
}
defineProps<Props>();
defineEmits<{ click: [] }>();

/**
 * Map a PrimeVue-style severity to a Tailwind badge palette.
 * @param severity
 */
function badgeClass(severity?: string): string {
  switch (severity) {
    case 'success':
      return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
    case 'warn':
      return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
    case 'danger':
      return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
    case 'info':
      return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
    default:
      return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300';
  }
}
</script>
