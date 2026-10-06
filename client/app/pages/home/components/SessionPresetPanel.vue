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
        <!-- The SESSION's own agent config, in the same three tabs the 预设 panel
             edits: 工具 / 中间件 are read-only here (change them in 菜单-预设), while
             子代理模型 is EDITABLE — a choice lands on the main agent's next turn
             through the same park/promote path as the main-model switch. -->
        <TabPanel
          value="agentTools"
          :header="t('config.agent.tabs.tools')">
          <div
            class="min-h-0 overflow-y-auto text-xs"
            data-test="session-preset-tools-tab">
            <div class="mb-2 flex items-center justify-between gap-2">
              <span class="text-gray-400">{{ t('config.agent.readonlyHint') }}</span>
              <span class="text-gray-400">
                {{
                  t('config.agent.tools.count', {
                    selected: sessionEnabledTools.length,
                    total: agentStore.catalog.tools.length
                  })
                }}
              </span>
            </div>
            <div
              v-for="group in agentStore.toolGroups"
              :key="group.group"
              class="mb-2 rounded-lg border border-solid border-gray-light p-2 dark:border-[#555]">
              <div class="mb-1 flex items-center justify-between gap-2">
                <span class="text-xs font-semibold text-gray-500 dark:text-gray-400">
                  {{ t(`config.agent.toolGroup.${group.group}`) }}
                </span>
                <span
                  v-if="agentGroupFullyRequired(group)"
                  class="inline-flex items-center gap-1 text-[11px] text-gray-400"
                  :data-test="`session-preset-tool-group-locked-${group.group}`">
                  <i class="pi pi-lock text-[9px]" />
                  {{ t('config.agent.tools.requiredHint') }}
                </span>
              </div>
              <!-- Same three row shapes as the editor: required locked on with a lock
                   glyph, a bulk-only group as plain chips, the rest as disabled
                   checkboxes (this whole tab is read-only). -->
              <div class="flex flex-wrap gap-x-4 gap-y-1">
                <label
                  v-for="tool in group.tools"
                  :key="tool.name"
                  class="flex items-center gap-1.5 text-gray-600 dark:text-gray-300"
                  :title="toolTooltip(tool)">
                  <Checkbox
                    v-if="!isBulkOnlyToolGroup(group.group)"
                    :model-value="sessionEnabledTools.includes(tool.name)"
                    binary
                    disabled
                    :data-test="`session-preset-tool-${tool.name}`" />
                  <i
                    v-if="tool.required"
                    class="pi pi-lock text-[9px] text-gray-400" />
                  <span
                    class="font-mono"
                    :class="{
                      'text-gray-400 line-through':
                        isBulkOnlyToolGroup(group.group) && !sessionEnabledTools.includes(tool.name)
                    }"
                    :data-test="isBulkOnlyToolGroup(group.group) ? `session-preset-tool-bulk-${tool.name}` : undefined">
                    {{ tool.name }}
                  </span>
                </label>
              </div>
            </div>
          </div>
        </TabPanel>

        <TabPanel
          value="agentMiddlewares"
          :header="t('config.agent.tabs.middlewares')">
          <div
            class="min-h-0 overflow-y-auto text-xs"
            data-test="session-preset-middlewares-tab">
            <div class="mb-2 text-gray-400">{{ t('config.agent.readonlyHint') }}</div>
            <div
              v-for="entry in agentStore.middlewares.gateable"
              :key="entry.name"
              class="mb-1 flex items-center justify-between gap-2 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
              <div class="min-w-0">
                <div class="truncate text-theme-main">{{ t(`config.agent.middleware.${entry.name}.name`) }}</div>
                <div class="truncate text-gray-400">{{ t(`config.agent.middleware.${entry.name}.desc`) }}</div>
              </div>
              <ToggleSwitch
                :model-value="!sessionDisabledMiddlewares.includes(entry.name)"
                disabled
                :data-test="`session-preset-middleware-${entry.name}`" />
            </div>
            <div class="mt-3">
              <div class="mb-1 text-xs font-semibold text-gray-500 dark:text-gray-400">
                {{ t('config.agent.middlewares.lockedTitle') }}
              </div>
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="entry in agentStore.middlewares.locked"
                  :key="entry.name"
                  class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500 dark:bg-gray-800 dark:text-gray-400">
                  <i class="pi pi-lock text-[9px]" />
                  {{ entry.name }}
                </span>
              </div>
            </div>
          </div>
        </TabPanel>

        <TabPanel
          value="agentSubagents"
          :header="t('config.agent.tabs.subagents')">
          <div
            class="min-h-0 overflow-y-auto text-xs"
            data-test="session-preset-subagents-tab">
            <div class="mb-2 flex items-center gap-2 text-gray-400">
              <span>{{ t('config.agent.subagents.sessionHint') }}</span>
              <span
                v-if="agentStore.isPending(sessionId)"
                class="inline-flex shrink-0 items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                data-test="session-preset-models-pending">
                <i class="pi pi-clock text-[9px]" />
                {{ t('sessionModelPicker.pending') }}
              </span>
            </div>
            <div
              v-for="role in agentStore.subagentRoles"
              :key="role.role"
              class="mb-2 flex items-center justify-between gap-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
              <div class="min-w-0">
                <div class="truncate text-theme-main">
                  {{ t(`config.agent.role.${role.role}`) }}
                  <span class="ml-1 font-mono text-[11px] text-gray-400">{{ role.role }}</span>
                </div>
                <div class="truncate text-gray-400">
                  {{
                    role.model_tier
                      ? t('config.agent.subagents.tier', { tier: role.model_tier })
                      : t('config.agent.subagents.tierDepth')
                  }}
                </div>
              </div>
              <Select
                :model-value="roleModelId(role.role)"
                :options="roleModelOptions"
                option-label="label"
                option-value="value"
                class="w-52"
                :data-test="`session-preset-role-model-${role.role}`"
                @update:model-value="value => setRoleModel(role.role, value)" />
            </div>
          </div>
        </TabPanel>

        <!-- 技能 tab: an EMPTY placeholder, mirroring the editor's tab set (a
             per-preset skill selection is not designed yet). -->
        <TabPanel
          value="agentSkills"
          :header="t('config.agent.tabs.skills')">
          <div
            class="min-h-0 overflow-y-auto text-xs"
            data-test="session-preset-skills-tab" />
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
import { FOLLOW_TIER_ID, isBulkOnlyToolGroup } from '~/stores/agent-config';
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

const llmProfiles = useLlmProfilesStore();

/** The session's EFFECTIVE agent config (the session is the live truth here). */
const sessionEnabledTools = computed<string[]>(() => (sessionId.value ? agentStore.enabledTools(sessionId.value) : []));
const sessionDisabledMiddlewares = computed<string[]>(() =>
  sessionId.value ? agentStore.disabledMiddlewares(sessionId.value) : []
);

/** Select options: "follow the role tier" + every env-config profile. */
const roleModelOptions = computed<Array<{ label: string; value: string }>>(() => [
  { label: t('config.agent.subagents.followTier'), value: FOLLOW_TIER_ID },
  ...llmProfiles.listFor('MAIN_LLM').map(profile => ({ label: profile.label, value: profile.id }))
]);

/**
 * Whether a group's membership is entirely required (nothing to switch anywhere,
 * so the header says so instead of showing a count).
 * @param group Catalogue group entry.
 * @param group.tools
 */
const agentGroupFullyRequired = (group: { tools: Array<{ required?: boolean }> }): boolean =>
  group.tools.length > 0 && group.tools.every(tool => tool.required === true);

/**
 * The hover text of one tool row: the backend description, plus the locked
 * explanation for a required tool.
 * @param tool Catalogue tool entry.
 * @param tool.name
 * @param tool.description
 * @param tool.required
 */
const toolTooltip = (tool: { name: string; description?: string; required?: boolean }): string => {
  const description = tool.description || tool.name;
  return tool.required === true ? `${description}\n${t('config.agent.tools.requiredHint')}` : description;
};

/**
 * The profile id a role currently points at ('' = follow the role tier).
 * @param role
 */
const roleModelId = (role: string): string =>
  agentStore.configOf(sessionId.value).subagent_models?.[role]?.id ?? FOLLOW_TIER_ID;

/**
 * Point one role at an env-config profile (or back at its tier) and write it to
 * the SESSION: the whole payload is merged (tools / middlewares preserved) and
 * the backend parks it until the turn boundary when a turn is in flight — the
 * same contract as switching the main model.
 * @param role Functional role.
 * @param profileId Chosen profile id ('' = follow the role tier).
 */
const setRoleModel = async (role: string, profileId: string): Promise<void> => {
  const sid = sessionId.value;
  if (!sid) return;
  const chosen = String(profileId ?? '');
  const profile = chosen && chosen !== FOLLOW_TIER_ID ? llmProfiles.byId('MAIN_LLM', chosen) : undefined;
  const current = agentStore.configOf(sid);
  const models = { ...(current.subagent_models ?? {}) };
  models[role] = profile ? llmProfiles.toSessionProfile(profile) : null;
  await agentStore.save(sid, { ...current, subagent_models: models });
};

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
    // The catalogue backs both the agent tabs and a built-in preset's agent
    // block (纯净 / 情感陪伴 derive theirs from it); loading is idempotent.
    await agentStore.loadCatalog();
    payload.value = await loadPresetPayload(entry, locale.value, t, presetCatalogFacts(agentStore));
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
    // The catalogue backs the tabs; the per-session config is what the three
    // agent tabs display (and 子代理模型 edits) — both are cheap and idempotent.
    void agentStore.loadCatalog();
    if (sessionId.value) void agentStore.hydrate(sessionId.value);
    void loadBinding();
  },
  { immediate: true }
);
watch(presets, () => {
  void loadBinding();
});
</script>
