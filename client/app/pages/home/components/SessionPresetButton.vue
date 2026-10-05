<template>
  <ToolbarPopover placement="down">
    <template #trigger="{ toggle }">
      <!-- Icon-only (like its top-bar neighbours); the title/aria carry the preset
           name, and at a squeezed column it hides with the folder button. -->
      <Button
        variant="text"
        icon="pi pi-user"
        :title="triggerTitle"
        :aria-label="triggerTitle"
        class="text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 @max-[300px]:hidden!"
        data-test="session-preset-trigger"
        @click="toggle" />
    </template>

    <div
      class="flex w-[360px] flex-col gap-2"
      data-test="session-preset-panel">
      <!-- Which preset this session was created with, then that preset's content —
           read-only: this control VIEWS the session's own preset and nothing else. -->
      <header class="flex flex-col gap-0.5 px-1">
        <span class="text-xs font-medium text-gray-500 dark:text-gray-400">{{ t('personaPreset.current') }}</span>
        <span
          class="text-sm text-theme-main"
          data-test="session-preset-bound">
          {{ boundName }}
        </span>
      </header>

      <Divider class="my-1! border-solid border-gray-100 dark:border-gray-800" />

      <div
        v-if="loadingPayload"
        class="flex items-center justify-center py-6">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>
      <p
        v-else-if="!payload"
        class="m-0 px-1 text-xs text-gray-400"
        data-test="session-preset-unavailable">
        {{ unavailableHint }}
      </p>
      <template v-else>
        <div class="px-1 text-xs font-medium text-gray-500 dark:text-gray-400">
          {{ t('personaPreset.contentTitle') }}
        </div>
        <TabView class="session-preset-preview">
          <TabPanel
            value="role"
            :header="t('config.tabs.role')">
            <div class="flex flex-col gap-1 text-xs">
              <div class="flex gap-1.5">
                <span class="text-gray-500 dark:text-gray-400">{{ t('config.role.assistant') }}</span>
                <span :class="payload.character.aiName ? '' : 'text-gray-400'">
                  {{ payload.character.aiName || t('personaPreset.empty') }}
                </span>
              </div>
              <div class="flex gap-1.5">
                <span class="text-gray-500 dark:text-gray-400">{{ t('config.role.userRole') }}</span>
                <span :class="payload.character.userName ? '' : 'text-gray-400'">
                  {{ payload.character.userName || t('personaPreset.empty') }}
                </span>
              </div>
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
              >{{ payload.content[item.file] || t('personaPreset.empty') }}</pre>
          </TabPanel>
        </TabView>
      </template>
    </div>
  </ToolbarPopover>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';
import type { SessionPresetBinding } from '@/composables/db';
import type { PersonaPresetPayload } from '@/composables/persona-catalog';
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

/** This session's preset binding (`null` = created before the binding existed). */
const binding = ref<SessionPresetBinding | null>(null);

/** The bound preset's payload — the only content this control shows. */
const payload = ref<PersonaPresetPayload | null>(null);
const loadingPayload = ref(false);

/** True when the bound preset no longer exists (a deleted saved preset). */
const boundPresetMissing = ref(false);

/**
 * The header label: the bound preset's name (stored at binding time, so a deleted
 * preset still reads right), or 未选择 for a session that has no binding.
 */
const boundName = computed(() => (binding.value ? binding.value.preset_name : t('personaPreset.unbound')));

/** Tooltip / accessible name of the icon-only trigger. */
const triggerTitle = computed(() =>
  binding.value ? t('personaPreset.currentWithName', { name: boundName.value }) : t('personaPreset.unbound')
);

/** Why there is nothing to show (no preset bound / its content is gone). */
const unavailableHint = computed(() =>
  boundPresetMissing.value ? t('personaPreset.missing') : t('personaPreset.unboundHint')
);

/** Read this session's binding and load exactly that preset's content. */
const loadBinding = async () => {
  payload.value = null;
  boundPresetMissing.value = false;
  try {
    binding.value = (await readCachedSessionPreset(props.sessionId)) ?? null;
  } catch (e) {
    logUtil.e('[SessionPresetButton] Failed to read the preset binding:', e);
    binding.value = null;
  }
  const bound = binding.value;
  if (!bound) return;
  loadingPayload.value = true;
  try {
    const entry = catalogEntries(presets.value).find(candidate => candidate.id === bound.preset_id);
    if (!entry) {
      // A saved preset that was deleted since: keep the name, no content to show.
      boundPresetMissing.value = true;
      return;
    }
    payload.value = await loadPresetPayload(entry, locale.value, t);
  } catch (e) {
    logUtil.e('[SessionPresetButton] Failed to load the preset content:', e);
    payload.value = null;
  } finally {
    loadingPayload.value = false;
  }
};

// The button follows the session it sits above (the shell re-renders it on
// navigation), the ACTIVE LOCALE (a built-in's content comes from the language
// template, and the i18n plugin settles the locale after first mount — watching
// only the session would keep the pre-settle language) and the saved presets (a
// deleted one flips to the missing hint).
watch(
  [() => props.sessionId, () => locale.value],
  () => {
    void loadBinding();
  },
  { immediate: true }
);
watch(presets, () => {
  void loadBinding();
});
</script>
