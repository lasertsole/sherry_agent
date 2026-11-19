<template>
  <div
    class="flex h-full flex-col items-center justify-center gap-1 px-4 py-12 text-center"
    data-test="not-implemented-panel">
    <i
      class="pi pi-box mb-2 text-4xl text-gray-300 dark:text-gray-600"
      aria-hidden="true"
      data-test="placeholder-icon" />
    <div
      class="text-sm font-medium text-gray-400 dark:text-gray-500"
      data-test="not-implemented-title">
      {{ t('common.notImplemented') }}
    </div>
    <div
      class="mt-1 text-xs text-gray-300 dark:text-gray-600"
      data-test="coming-soon-hint">
      {{ t('common.comingSoon') }}
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Shared placeholder for navigation items whose backend / panel is not shipped
 * yet (UI upgrade S7). A nav item is registered with this component so clicking
 * it shows a friendly message instead of a blank column; once the real panel is
 * ready the nav item's `component` is swapped without changing id/icon/label.
 */
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
</script>
