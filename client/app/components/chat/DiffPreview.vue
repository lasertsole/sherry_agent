<template>
  <div
    class="overflow-x-auto rounded border border-solid border-gray-200 bg-white font-mono text-xs dark:border-gray-600 dark:bg-gray-900/60"
    data-test="tool-diff">
    <div
      class="flex gap-2 border-b border-solid border-gray-200 px-2 py-1 text-[11px] tabular-nums dark:border-gray-600">
      <span
        v-if="newFile"
        class="rounded bg-green-100 px-1.5 text-green-700 dark:bg-green-900/40 dark:text-green-300">
        {{ t('chatBox.diffNewFile') }}
      </span>
      <span class="text-green-600 dark:text-green-400">+{{ diff.addedCount }}</span>
      <span class="text-red-600 dark:text-red-400">−{{ diff.removedCount }}</span>
    </div>
    <div
      v-for="(line, index) in diff.lines"
      :key="index"
      class="flex border-l-2"
      :class="rowClass(line.type)">
      <span class="w-10 shrink-0 select-none pr-1 text-right text-gray-400 dark:text-gray-500">
        {{ line.oldLineNo ?? '' }}
      </span>
      <span class="w-10 shrink-0 select-none pr-2 text-right text-gray-400 dark:text-gray-500">
        {{ line.newLineNo ?? '' }}
      </span>
      <span class="w-4 shrink-0 select-none text-center">{{ prefixFor(line.type) }}</span>
      <span class="min-w-0 flex-1 whitespace-pre-wrap break-all">{{ line.content }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Inline diff preview for tool results (patch_file / write_file): one row per
 * classified line with old/new line-number gutters, a prefix column, and the
 * GitDiffPanel palette (green added / red removed / blue hunk headers). Pure
 * presentation — the parsing lives in `app/utils/tool-diff.ts`.
 */
import { useI18n } from 'vue-i18n';
import type { DiffLine, ToolDiff } from '~/utils/tool-diff';

defineProps<{
  /** The parsed diff rows (already fail-open upstream). */
  diff: ToolDiff;
  /** A new file's all-added preview draws the badge instead of a −count. */
  newFile?: boolean;
}>();

const { t } = useI18n();

function rowClass(type: DiffLine['type']): string {
  switch (type) {
    case 'added':
      return 'bg-green-50 border-green-500 dark:bg-green-900/25 dark:border-green-600';
    case 'removed':
      return 'bg-red-50 border-red-500 dark:bg-red-900/25 dark:border-red-600';
    case 'hunk':
      return 'bg-blue-50 border-transparent text-blue-600 dark:bg-blue-900/25 dark:text-blue-400';
    default:
      return 'border-transparent';
  }
}

function prefixFor(type: DiffLine['type']): string {
  switch (type) {
    case 'added':
      return '+';
    case 'removed':
      return '-';
    default:
      return ' ';
  }
}
</script>
