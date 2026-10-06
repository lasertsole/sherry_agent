<template>
  <div
    class="flex h-full min-h-0 flex-col gap-3 p-4"
    data-test="session-preset-panel">
    <!-- The session's preset, read-only: which one it was created with, then that
         preset's content in the same four tabs the 预设 panel edits. Nothing here
         can be selected or switched — creation is the only place a preset is
         chosen. -->
    <header class="flex flex-col gap-0.5">
      <span class="text-xs font-medium text-gray-500 dark:text-gray-400">{{ t('personaPreset.current') }}</span>
      <span
        class="text-sm text-theme-main"
        data-test="session-preset-bound">
        {{ boundName }}
      </span>
    </header>

    <Divider class="my-0! border-solid border-gray-100 dark:border-gray-800" />

    <div
      v-if="loadingPayload"
      class="flex flex-1 items-center justify-center">
      <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
    </div>
    <p
      v-else-if="!payload"
      class="m-0 text-xs text-gray-400"
      data-test="session-preset-unavailable">
      {{ unavailableHint }}
    </p>
    <div
      v-else
      class="flex min-h-0 flex-1 flex-col gap-2">
      <div class="text-xs font-medium text-gray-500 dark:text-gray-400">
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
            class="m-0 overflow-auto whitespace-pre-wrap break-words text-xs"
            :data-test="`session-preset-content-${item.file}`"
            >{{ payload.content[item.file] || t('personaPreset.empty') }}</pre>
        </TabPanel>
        <!-- The preset's 工具 / 中间件 / 子代理模型 selection, summarised read-only
             (the session's own config is the live truth; this is what the preset
             carries and writes on apply). -->
        <TabPanel
          value="agent"
          :header="t('config.agent.tabs.tools')">
          <div
            class="flex flex-col gap-1 text-xs"
            data-test="session-preset-agent-summary">
            <div class="flex gap-1.5">
              <span class="text-gray-500 dark:text-gray-400">{{ t('config.agent.tabs.tools') }}</span>
              <span>{{ agentSummary.tools }}</span>
            </div>
            <div class="flex gap-1.5">
              <span class="text-gray-500 dark:text-gray-400">{{ t('config.agent.tabs.middlewares') }}</span>
              <span>{{ agentSummary.middlewares }}</span>
            </div>
            <div class="flex gap-1.5">
              <span class="text-gray-500 dark:text-gray-400">{{ t('config.agent.tabs.subagents') }}</span>
              <span>{{ agentSummary.models }}</span>
            </div>
          </div>
        </TabPanel>
      </TabView>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import type { SessionPresetBinding } from '@/composables/db';
import type { PersonaPresetPayload } from '@/composables/persona-catalog';
import { logUtil } from '~/utils/log';

const { t, locale } = useI18n();
const route = useRoute();
const agentStore = useAgentConfigStore();

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

/**
 * Read-only summary of the preset's agent block: the tool count (the total comes
 * from the catalogue, so "no config" reads as every tool), the disabled
 * middleware names and the per-role model labels.
 */
const agentSummary = computed<{ tools: string; middlewares: string; models: string }>(() => {
  const block = payload.value?.agent ?? {};
  const total = agentStore.catalog.tools.length;
  const selected = Array.isArray(block.tools) ? block.tools.length : total;
  const disabled = block.middlewares_disabled ?? [];
  const models = Object.entries(block.subagent_models ?? {})
    .filter(([, profile]) => !!profile)
    .map(([role, profile]) => `${role} → ${profile?.label || profile?.model || ''}`);
  return {
    tools: t('config.agent.viewer.tools', { selected, total }),
    middlewares: disabled.length
      ? t('config.agent.viewer.middlewares', { names: disabled.join(', ') })
      : t('config.agent.viewer.middlewaresNone'),
    models: models.length ? t('config.agent.viewer.models', { names: models.join('; ') }) : '—'
  };
});

/** Session whose binding is shown — the tab stays open across session switches. */
const sessionId = computed(() => (typeof route.params.sid === 'string' ? route.params.sid : ''));

/** This session's preset binding (`null` = created before the binding existed). */
const binding = ref<SessionPresetBinding | null>(null);

/** The bound preset's payload — the only content this panel shows. */
const payload = ref<PersonaPresetPayload | null>(null);
const loadingPayload = ref(false);

/** True when the bound preset no longer exists (a deleted saved preset). */
const boundPresetMissing = ref(false);

/**
 * The header label: the bound preset's name (stored at binding time, so a deleted
 * preset still reads right), or 未选择 for a session that has no binding.
 */
const boundName = computed(() => (binding.value ? binding.value.preset_name : t('personaPreset.unbound')));

/** Why there is nothing to show (no preset bound / its content is gone). */
const unavailableHint = computed(() =>
  boundPresetMissing.value ? t('personaPreset.missing') : t('personaPreset.unboundHint')
);

/** Read the session's binding and load exactly that preset's content. */
const loadBinding = async () => {
  payload.value = null;
  boundPresetMissing.value = false;
  if (!sessionId.value) {
    binding.value = null;
    return;
  }
  try {
    binding.value = (await readCachedSessionPreset(sessionId.value)) ?? null;
  } catch (e) {
    logUtil.e('[SessionPresetPanel] Failed to read the preset binding:', e);
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
    logUtil.e('[SessionPresetPanel] Failed to load the preset content:', e);
    payload.value = null;
  } finally {
    loadingPayload.value = false;
  }
};

// The panel follows the ACTIVE SESSION (the tab outlives session switches), the
// ACTIVE LOCALE (a built-in's content comes from the language template, and the
// i18n plugin settles the locale after first mount — watching only the session
// would keep the pre-settle language) and the saved presets (a deleted one flips
// to the missing hint).
watch(
  [sessionId, () => locale.value],
  () => {
    // The catalogue backs the agent summary's "n / total" tool count; both are
    // cheap and idempotent, so the watcher reloads them with the binding.
    void agentStore.loadCatalog();
    void loadBinding();
  },
  { immediate: true }
);
watch(presets, () => {
  void loadBinding();
});
</script>
