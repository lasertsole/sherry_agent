<template>
  <ToolbarPopover>
    <template #trigger="{ toggle }">
      <!-- Collapsed chip: the folder glyph plus the bound directory's basename.
           Unbound sessions carry a warning tone instead of a path, so "the agent
           is working in the sherry checkout" is visible at a glance rather than
           buried in a setting. A parked choice shows the clock icon (same visual
           language as the model/thinking controls). -->
      <div class="flex items-center gap-1">
        <button
          type="button"
          data-test="project-dir-trigger"
          class="flex items-center gap-1 rounded px-1.5 py-0.5 text-xs transition-colors"
          :class="
            bound
              ? 'text-theme-main hover:bg-gray-100 dark:hover:bg-gray-800'
              : 'text-amber-500 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-900/20'
          "
          :title="triggerTitle"
          :aria-label="triggerTitle"
          @click="onOpen(toggle)">
          <i :class="bound ? 'pi pi-folder' : 'pi pi-exclamation-triangle'"></i>
          <span class="max-w-[10rem] truncate">{{ label }}</span>
        </button>
        <i
          v-if="state.pendingDirectory"
          data-test="project-dir-pending"
          class="pi pi-clock text-xs text-theme-main"
          :title="t('toolbar.projectDirectory.pendingHint')"
          :aria-label="t('toolbar.projectDirectory.pendingHint')"></i>
      </div>
    </template>

    <div
      class="flex w-80 flex-col gap-2 p-1"
      data-test="project-dir-panel">
      <p class="m-0 text-xs font-medium text-gray-500 dark:text-gray-400">
        {{ t('toolbar.projectDirectory.label') }}
      </p>
      <p
        class="m-0 break-all text-xs text-gray-600 dark:text-gray-300"
        data-test="project-dir-effective">
        {{ state.effective || t('toolbar.projectDirectory.unbound') }}
        <span
          v-if="!bound"
          class="text-amber-500 dark:text-amber-400"
          >（{{ t('toolbar.projectDirectory.unbound') }}）</span
        >
      </p>
      <p
        v-if="state.pendingDirectory"
        class="m-0 break-all text-xs text-theme-main">
        {{ t('toolbar.projectDirectory.pendingLabel') }}：{{ state.pendingDirectory }}
      </p>

      <div class="flex flex-col gap-1">
        <!-- Manual entry is a first-class channel (the browser build has no native
             picker); the server validates existence and containment. -->
        <InputText
          v-model="manualPath"
          class="w-full font-mono text-xs"
          :placeholder="t('toolbar.projectDirectory.manualPlaceholder')"
          data-test="project-dir-input"
          spellcheck="false"
          autocomplete="off" />
        <div class="flex justify-end gap-2">
          <Button
            v-if="canBrowse"
            :label="t('toolbar.projectDirectory.browse')"
            icon="pi pi-folder-open"
            size="small"
            severity="secondary"
            text
            data-test="project-dir-browse"
            @click="browse" />
          <Button
            :label="t('toolbar.projectDirectory.confirm')"
            icon="pi pi-check"
            size="small"
            :disabled="!manualPath.trim()"
            data-test="project-dir-confirm"
            @click="confirmManual" />
        </div>
      </div>

      <p
        v-if="state.error"
        class="m-0 break-all text-xs text-red-500 dark:text-red-400"
        data-test="project-dir-error">
        {{ state.error }}
      </p>
    </div>
  </ToolbarPopover>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
// Siblings under pages/home/components are NOT auto-imported (only app/components
// is), so every component usage needs an explicit import.
import ToolbarPopover from './ToolbarPopover.vue';

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
/** Per-session project-directory control store (hydrated from the backend). */
const store = useProjectDirectoryStore();

/** Manual path input (the popover's editable field). */
const manualPath = ref('');

const state = computed(() => store.stateFor(props.sessionId));
const bound = computed(() => !!state.value.directory);
const label = computed(() => {
  const dir = state.value.directory;
  if (!dir) return t('toolbar.projectDirectory.unbound');
  const parts = dir.replace(/\/+$/, '').split('/');
  return parts[parts.length - 1] || dir;
});
const triggerTitle = computed(() => {
  const dir = state.value.directory;
  return dir ? dir : `${t('toolbar.projectDirectory.unbound')}：${state.value.effective}`;
});

/**
 * Whether the Tauri-native folder picker is usable in this build. The browser
 * fallback (manual entry) always exists, so this only hides a button.
 */
const canBrowse = computed(() => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window);

/**
 * Re-hydrate and prefill the input whenever the panel opens (the server is the
 * only authority on what is currently bound).
 * @param toggle
 */
const onOpen = (toggle: () => void): void => {
  store.clearError(props.sessionId);
  void store.hydrate(props.sessionId);
  manualPath.value = state.value.directory ?? '';
  toggle();
};

/** Open the native folder picker (Tauri only) and apply the picked path. */
const browse = async (): Promise<void> => {
  try {
    const picked = await pickProjectDirectory();
    if (!picked) return;
    await store.select(props.sessionId, picked);
    manualPath.value = store.stateFor(props.sessionId).directory ?? picked;
  } catch (e) {
    store.fail(props.sessionId, e instanceof Error ? e.message : String(e));
  }
};

/** Submit the manually typed absolute path (the server validates it). */
const confirmManual = async (): Promise<void> => {
  const value = manualPath.value.trim();
  if (!value) return;
  if (!value.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(value)) {
    store.fail(props.sessionId, t('toolbar.projectDirectory.absoluteRequired'));
    return;
  }
  await store.select(props.sessionId, value);
};

/** Keep the chip honest when the session id changes (KeepAlive instances). */
watch(
  () => props.sessionId,
  sid => {
    if (sid) void store.hydrate(sid);
  },
  { immediate: true }
);
</script>
