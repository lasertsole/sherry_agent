<template>
  <!-- Right-sidebar tab body: mounted and unmounted with its tab (the sidebar
       owns the tab label and the close button). The interface language is a
       browser-profile setting, not a session one, so the choice applies at once
       and survives a reload — nuxt-i18n persists it itself. -->
  <div class="flex flex-col gap-3 h-full min-h-0 p-4">
    <div class="flex flex-col gap-1">
      <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
        {{ t('config.i18n.title') }}
      </span>
      <span class="text-xs text-gray-400 dark:text-gray-500">
        {{ t('config.i18n.hint') }}
      </span>
    </div>

    <div
      class="flex flex-1 min-h-0 flex-col gap-2 overflow-y-auto"
      role="radiogroup"
      :aria-label="t('config.i18n.title')">
      <button
        v-for="option in options"
        :key="option.code"
        type="button"
        role="radio"
        :aria-checked="option.code === current"
        :data-test="`i18n-option-${option.code}`"
        :class="[
          'flex items-center justify-between gap-3 rounded-lg border border-solid px-3 py-2 text-left transition-colors',
          option.code === current
            ? 'border-theme-main bg-gray-50 dark:bg-gray-800'
            : 'border-gray-200 hover:border-theme-main dark:border-gray-700'
        ]"
        @click="switchTo(option.code)">
        <span class="flex min-w-0 items-center gap-2">
          <span class="truncate text-sm text-gray-700 dark:text-gray-200">{{ option.name }}</span>
          <span class="shrink-0 text-xs text-gray-400 dark:text-gray-500">{{ option.code }}</span>
        </span>
        <i
          v-if="option.code === current"
          class="pi pi-check shrink-0 text-theme-main" />
      </button>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';

// `useLocaleSwitch` comes from Nuxt's composable auto-import (a value import of
// `~/composables/**` is lint-restricted), same as the other composables here.

const { t } = useI18n();
// The shared switch (setLocale + the preference cookie app.vue reads at boot).
// Each locale is named in its OWN language: a person looking for theirs should
// not have to read the current UI language first.
const { current, switchTo, options } = useLocaleSwitch();
</script>
