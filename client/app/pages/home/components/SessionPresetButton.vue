<template>
  <ToolbarPopover placement="down">
    <template #trigger="{ toggle }">
      <Button
        variant="text"
        size="small"
        icon="pi pi-user"
        :label="boundName"
        icon-pos="left"
        :title="`${t('personaPreset.current')}：${boundName}`"
        :aria-label="`${t('personaPreset.current')}：${boundName}`"
        data-test="session-preset-trigger"
        @click="toggle" />
    </template>

    <div
      class="flex w-[360px] flex-col gap-2"
      data-test="session-preset-panel">
      <!-- Which preset this session was created with (未选择 for sessions created
           before the binding existed). -->
      <header class="flex items-center justify-between gap-2 px-1 text-xs font-medium text-gray-500 dark:text-gray-400">
        <span>{{ t('personaPreset.current') }}</span>
        <span
          class="truncate text-theme-main"
          data-test="session-preset-bound">
          {{ boundName }}
        </span>
      </header>

      <!-- The catalogue: click an entry to preview its content below. -->
      <div
        class="flex max-h-[30vh] flex-col gap-1 overflow-y-auto"
        data-test="session-preset-list">
        <div
          v-for="entry in entries"
          :key="entry.id"
          role="button"
          tabindex="0"
          class="flex cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm text-theme-main"
          :class="selectedId === entry.id ? 'bg-[#c1d6e5]!' : 'hover:bg-gray-100 dark:hover:bg-[#2a2a36]'"
          :data-test="`session-preset-entry-${entry.id}`"
          @click="select(entry)">
          <span class="min-w-0 truncate">{{ entryName(entry, t) }}</span>
          <span class="flex shrink-0 items-center gap-1.5">
            <i
              v-if="boundId === entry.id"
              class="pi pi-check text-xs text-theme-main"
              :title="t('personaPreset.current')"
              aria-hidden="true"></i>
            <span
              v-if="entry.badgeKey"
              class="rounded-full bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {{ t(entry.badgeKey) }}
            </span>
          </span>
        </div>
      </div>

      <Divider class="my-1! border-solid border-gray-100 dark:border-gray-800" />

      <!-- The selected preset's content, read-only, in the same four tabs the
           预设 panel edits — this is the "查看" surface. -->
      <div class="px-1 text-xs font-medium text-gray-500 dark:text-gray-400">
        {{ t('personaPreset.contentTitle') }}
      </div>
      <div
        v-if="loadingPayload"
        class="flex items-center justify-center py-6">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>
      <TabView
        v-else
        class="session-preset-preview">
        <TabPanel
          value="role"
          :header="t('config.tabs.role')">
          <div class="flex flex-col gap-1 text-xs">
            <span>{{ t('config.role.assistant') }}：{{ payload?.character.aiName || t('personaPreset.empty') }}</span>
            <span>{{ t('config.role.userRole') }}：{{ payload?.character.userName || t('personaPreset.empty') }}</span>
          </div>
        </TabPanel>
        <TabPanel
          v-for="item in PREVIEW_FILES"
          :key="item.file"
          :value="item.tabKey"
          :header="t(`config.tabs.${item.tabKey}`)">
          <pre
            class="m-0 max-h-[28vh] overflow-auto whitespace-pre-wrap break-words text-xs"
            :data-test="`session-preset-content-${item.file}`"
            >{{ payload?.content[item.file] || t('personaPreset.empty') }}</pre>
        </TabPanel>
      </TabView>
    </div>
  </ToolbarPopover>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';
import type { SessionPresetBinding } from '@/composables/db';
import type { PersonaPresetPayload, PresetEntry } from '@/composables/persona-catalog';
import { logUtil } from '~/utils/log';

const props = defineProps<{ sessionId: string }>();

const { t, locale } = useI18n();

/**
 * The persona files previewed as tabs, each with the i18n key suffix of its tab
 * header (the same `config.tabs.*` labels the 预设 panel uses).
 */
const PREVIEW_FILES: ReadonlyArray<{ file: string; tabKey: string }> = [
  { file: 'AGENTS.md', tabKey: 'agents' },
  { file: 'SOUL.md', tabKey: 'soul' },
  { file: 'USER.md', tabKey: 'user' }
];

/** The saved presets (shared singleton); built-ins come from the catalogue. */
const { presets } = usePersonaPresets();

/** Built-ins + saved presets, in the shared display order. */
const entries = computed(() => catalogEntries(presets.value));

/** This session's preset binding (`null` = created before the binding existed). */
const binding = ref<SessionPresetBinding | null>(null);

/** Entry currently previewed in the tabs (starts at the session's own preset). */
const selectedId = ref<string>(DEFAULT_PRESET_ID);

/** Payload of the previewed entry (null while loading / on failure). */
const payload = ref<PersonaPresetPayload | null>(null);
const loadingPayload = ref(false);

/** The bound preset id, `null` when the session has none. */
const boundId = computed(() => binding.value?.preset_id ?? null);

/**
 * The trigger/header label: the bound preset's current name when it still
 * exists, else the name stored at binding time (a deleted user preset).
 */
const boundName = computed(() => {
  const current = binding.value;
  if (!current) return t('personaPreset.unbound');
  const entry = entries.value.find(candidate => candidate.id === current.preset_id);
  return entry ? entryName(entry, t) : current.preset_name;
});

/** Load the selected entry's payload for the preview tabs. */
const loadPreview = async () => {
  const entry = entries.value.find(candidate => candidate.id === selectedId.value);
  if (!entry) {
    payload.value = null;
    return;
  }
  loadingPayload.value = true;
  try {
    payload.value = await loadPresetPayload(entry, locale.value, t);
  } catch (e) {
    logUtil.e('[SessionPresetButton] Failed to load the preset preview:', e);
    payload.value = null;
  } finally {
    loadingPayload.value = false;
  }
};

/**
 * Click an entry: preview that preset (view-only — the session's own binding and
 * the persona files are untouched).
 * @param entry Catalogue entry to preview.
 */
const select = (entry: PresetEntry) => {
  selectedId.value = entry.id;
  void loadPreview();
};

/** Read this session's binding, then preview it. */
const loadBinding = async () => {
  try {
    binding.value = (await readCachedSessionPreset(props.sessionId)) ?? null;
  } catch (e) {
    logUtil.e('[SessionPresetButton] Failed to read the preset binding:', e);
    binding.value = null;
  }
  selectedId.value = binding.value?.preset_id ?? DEFAULT_PRESET_ID;
  await loadPreview();
};

// The button follows the session it sits above (the shell re-renders it on
// navigation; a re-binding within the same session is picked up on remount).
watch(() => props.sessionId, loadBinding, { immediate: true });
watch(entries, () => {
  void loadPreview();
});
</script>
