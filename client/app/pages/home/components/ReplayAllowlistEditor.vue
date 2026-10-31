<script setup lang="ts">
/**
 * The replay-gate allowlist editor (系统配置 → 应用配置).
 *
 * The finish-time regression gate re-runs a flow's recorded verification
 * commands, but only for a recognized verifier toolchain (five are built in).
 * A user on another toolchain (gradle / maven / dotnet / rspec / swift) adds
 * the executable NAME here instead of editing code; the backend merges these
 * entries over its defaults, so the built-ins can never be removed from this
 * panel — it only ever shows what the user added.
 */
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import { useI18n } from 'vue-i18n';
// Explicit import (not the auto-import): the component's test mounts it without
// the unimport injection, so bare symbols would be undefined there.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { readSherryList, writeSherryList } from '~/composables/sherryConfig';
import { logUtil } from '~/utils/log';

const { t } = useI18n();

/** Shell metacharacters and the entry shape the backend accepts. */
const ENTRY_RE = /^[^\s;&|<>$`]+$/;

const entries = ref<string[]>([]);
const draft = ref('');
const loading = ref(false);
const saving = ref(false);
const error = ref('');

/** Why the draft cannot be added yet ('' when it can). */
const draftError = computed(() => {
  const value = draft.value.trim().toLowerCase();
  if (!value) return '';
  if (value.length > 256) return t('config.sherry.allowlist.tooLong');
  if (!ENTRY_RE.test(value)) return t('config.sherry.allowlist.badChars');
  if (entries.value.includes(value)) return t('config.sherry.allowlist.duplicate');
  return '';
});

/**
 * Load the user's entries (the defaults are the backend's, never shown here).
 */
async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    entries.value = await readSherryList('regression_gate_allowed_executables');
  } catch (e) {
    logUtil.w('[ReplayAllowlist] read failed:', e);
    error.value = t('config.sherry.allowlist.loadFailed');
  } finally {
    loading.value = false;
  }
}

/**
 * Persist the current list; the backend answers what it actually wrote.
 */
async function save(): Promise<void> {
  saving.value = true;
  error.value = '';
  try {
    entries.value = await writeSherryList('regression_gate_allowed_executables', entries.value);
  } catch (e) {
    logUtil.w('[ReplayAllowlist] write failed:', e);
    error.value = t('config.sherry.allowlist.saveFailed');
  } finally {
    saving.value = false;
  }
}

/** Add the typed entry (normalized) and persist. */
async function addEntry(): Promise<void> {
  const value = draft.value.trim().toLowerCase();
  if (!value || draftError.value) return;
  entries.value = [...entries.value, value];
  draft.value = '';
  await save();
}

/**
 * Remove one entry and persist.
 * @param value
 */
async function removeEntry(value: string): Promise<void> {
  entries.value = entries.value.filter(entry => entry !== value);
  await save();
}

onMounted(load);
</script>

<template>
  <div
    class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3"
    data-test="replay-allowlist">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="m-0 text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('config.sherry.allowlist.title') }}
        </p>
        <p class="m-0 mt-1 text-xs text-gray-500 dark:text-gray-400">
          {{ t('config.sherry.allowlist.hint') }}
        </p>
      </div>
      <Button
        size="small"
        severity="secondary"
        text
        icon="pi pi-refresh"
        :loading="loading"
        :label="t('config.sherry.allowlist.reload')"
        @click="load" />
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <InputText
        v-model="draft"
        class="w-48 font-mono text-xs"
        :placeholder="t('config.sherry.allowlist.placeholder')"
        autocomplete="off"
        spellcheck="false"
        data-test="replay-allowlist-input"
        @keydown.enter.prevent="addEntry" />
      <Button
        size="small"
        icon="pi pi-plus"
        :label="t('config.sherry.allowlist.add')"
        :disabled="!draft.trim() || !!draftError || saving"
        :loading="saving"
        data-test="replay-allowlist-add"
        @click="addEntry" />
    </div>
    <p
      v-if="draftError"
      class="m-0 text-xs text-amber-600 dark:text-amber-400">
      {{ draftError }}
    </p>
    <p
      v-if="error"
      class="m-0 text-xs text-red-600 dark:text-red-400">
      {{ error }}
    </p>

    <div
      v-if="entries.length"
      class="flex flex-wrap gap-1">
      <span
        v-for="entry in entries"
        :key="entry"
        class="flex items-center gap-1 rounded bg-gray-100 px-2 py-0.5 font-mono text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
        {{ entry }}
        <button
          type="button"
          class="flex items-center border-0 bg-transparent p-0 text-gray-400 hover:text-red-500"
          :aria-label="t('config.sherry.allowlist.remove')"
          @click="removeEntry(entry)">
          <i class="pi pi-times text-[10px]" />
        </button>
      </span>
    </div>
    <p
      v-else-if="!loading"
      class="m-0 text-xs text-gray-400"
      data-test="replay-allowlist-empty">
      {{ t('config.sherry.allowlist.empty') }}
    </p>
  </div>
</template>
