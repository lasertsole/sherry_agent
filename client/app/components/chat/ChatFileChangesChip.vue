<template>
  <!-- "This turn changed N files · undo" — the chip that opens the revert
       dialog. Deliberately NOT hidden when a revert is impossible: a disabled
       chip is honest ("there were changes, they cannot be undone now"), a
       missing one looks like the feature never existed. -->
  <button
    type="button"
    :class="[
      'flex items-center gap-1.5 rounded-full border border-solid px-2.5 py-1 text-xs transition-colors cursor-pointer select-none',
      canRevert
        ? 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100 dark:border-amber-800/60 dark:bg-amber-950/40 dark:text-amber-300 dark:hover:bg-amber-950/70'
        : 'border-gray-200 bg-gray-50 text-gray-400 cursor-not-allowed dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-500'
    ]"
    :disabled="!canRevert"
    :title="canRevert ? revertLabel : unrevertableLabel"
    @click="$emit('open')">
    <span class="pi pi-undo text-[0.7rem]"></span>
    <span>{{ fileCount }}</span>
    <span class="opacity-70">·</span>
    <span>{{ canRevert ? revertLabel : unrevertableLabel }}</span>
  </button>
</template>

<script setup lang="ts">
interface Props {
  /** How many distinct files the session's writes touched */
  fileCount: number;
  /** Whether a revert is still possible (drives the disabled state) */
  canRevert: boolean;
  /** Localized "撤销" label */
  revertLabel: string;
  /** Localized "不可撤销" label */
  unrevertableLabel: string;
}
defineProps<Props>();
defineEmits<{ open: [] }>();
</script>

<i18n lang="json">
{
  "en": {
    "revert": "Undo changes",
    "unrevertable": "Changes not revertable"
  },
  "zh": {
    "revert": "撤销改动",
    "unrevertable": "改动不可撤销"
  },
  "ja": {
    "revert": "変更を元に戻す",
    "unrevertable": "変更は元に戻せません"
  },
  "ko": {
    "revert": "변경 취소",
    "unrevertable": "변경을 되돌릴 수 없음"
  }
}
</i18n>
