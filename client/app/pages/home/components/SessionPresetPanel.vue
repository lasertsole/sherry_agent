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
    <div
      v-else
      class="flex min-h-0 flex-1 flex-col gap-2">
      <div
        v-if="payload"
        class="text-xs font-medium text-gray-500 dark:text-gray-400">
        {{ t('personaPreset.contentTitle') }}
      </div>
      <p
        v-else
        class="m-0 text-xs text-gray-400"
        data-test="session-preset-unavailable">
        {{ unavailableHint }}
      </p>
      <TabView class="session-preset-preview">
        <!-- Agent models tab (FIRST: the session's own model pair is what this viewer is
             opened for): 主代理 mirrored read-only from the session-model control,
             子代理 editable — a change parks until the next turn. -->
        <TabPanel
          v-if="payload"
          value="agentSubagents"
          :header="t('config.agent.tabs.subagents')">
          <div
            class="flex min-h-0 min-w-0 flex-col gap-2 text-xs"
            data-test="session-preset-models-tab">
            <div class="flex items-center gap-1">
              <button
                v-for="scope in AGENT_MODEL_SCOPES"
                :key="scope"
                type="button"
                class="rounded-full px-2.5 py-0.5 text-xs transition-colors"
                :class="
                  modelScope === scope
                    ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
                    : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                "
                :data-test="`session-preset-models-scope-${scope}`"
                @click="modelScope = scope">
                {{ t(`config.agent.models.scope_${scope}`) }}
              </button>
            </div>
            <template v-if="modelScope === 'main'">
              <div class="mb-2 flex items-center gap-2 text-gray-400">
                <span>{{ t('config.agent.models.mainHint') }}</span>
                <span
                  v-if="sessionModel.isPending(sessionId)"
                  class="inline-flex shrink-0 items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                  data-test="session-preset-main-model-pending">
                  <i class="pi pi-clock text-[9px]" />
                  {{ t('sessionModelPicker.pending') }}
                </span>
              </div>
              <div
                class="flex items-center justify-between gap-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
                <span class="min-w-0 truncate text-theme-main">{{ mainModelLabel }}</span>
                <!-- The SAME control as the toolbar's model switch (one store, one
                     endpoint): a change here moves that picker, and vice versa. -->
                <Select
                  :model-value="sessionModel.currentId(sessionId)"
                  :options="mainModelOptions"
                  option-label="label"
                  option-value="value"
                  class="w-52"
                  data-test="session-preset-main-model"
                  @update:model-value="chooseMainModel" />
              </div>
            </template>
            <template v-else>
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
                <div class="flex shrink-0 flex-col items-stretch gap-1">
                  <Select
                    :model-value="roleModelId(role.role)"
                    :options="roleModelOptions"
                    option-label="label"
                    option-value="value"
                    class="w-52"
                    :data-test="`session-preset-role-model-${role.role}`"
                    @update:model-value="value => setRoleModel(role.role, value)" />
                  <label
                    v-if="roleModelId(role.role) !== FOLLOW_TIER_ID"
                    class="flex items-center justify-between gap-1 text-[11px] text-gray-400">
                    <span>{{ t('config.agent.subagents.maxOutput') }}</span>
                    <input
                      type="number"
                      min="8192"
                      step="1024"
                      class="w-24 rounded border border-solid border-gray-light bg-transparent px-1.5 py-0.5 text-right text-xs text-gray-600 outline-none focus:border-theme-main dark:border-[#555] dark:text-gray-300"
                      :value="roleMaxOutput(role.role)"
                      :title="t('config.agent.subagents.maxOutputHint')"
                      :aria-label="t('config.agent.subagents.maxOutput')"
                      :data-test="`session-preset-role-max-output-${role.role}`"
                      @change="event => onRoleMaxOutputChange(role.role, event)" />
                  </label>
                </div>
              </div>
            </template>
            <!-- Advanced: the per-session custom thinking wire mapping
                 (escape hatch for providers the built-in formats miss). -->
            <ThinkingCustomMappingPanel :session-id="sessionId" />
          </div>
        </TabPanel>

        <TabPanel
          v-if="payload"
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
          v-for="item in visiblePreviewFiles"
          :key="item.file"
          :value="item.tabKey"
          :header="t(`config.tabs.${item.tabKey}`)">
          <pre
            class="m-0 overflow-auto whitespace-pre-wrap break-words text-xs"
            :data-test="`session-preset-content-${item.file}`"
            >{{ payload?.content[item.file] || t('personaPreset.empty') }}</pre>
        </TabPanel>
        <!-- The SESSION's own agent config, in the same tabs the Presets & Role
             panel edits: skills are read-only here (change them in 菜单-预设角色),
             while the model pickers are EDITABLE — a choice lands on the main
             agent's next turn through the same park/promote path as the
             main-model switch. Built-in tools and the middleware chain are not
             configurable at all. -->
        <!-- Skills tab: which skill index entries the SESSION keeps in its system
             prompt (read-only here — the 预设 panel edits it). -->
        <TabPanel
          v-if="payload"
          value="agentSkills"
          :header="t('config.agent.tabs.skills')">
          <div
            class="min-h-0 min-w-0 overflow-y-auto text-xs"
            data-test="session-preset-skills-tab">
            <div class="mb-2 flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
              <span class="min-w-0 flex-1 text-gray-400">{{ t('config.agent.readonlyHint') }}</span>
              <span class="shrink-0 text-gray-400">
                {{
                  t('config.agent.skills.count', {
                    selected: sessionEnabledSkills.length,
                    total: agentStore.catalog.skills.length
                  })
                }}
              </span>
            </div>
            <div class="mb-2 flex items-center gap-1">
              <button
                v-for="scope in AGENT_SKILL_SCOPES"
                :key="scope"
                type="button"
                class="rounded-full px-2.5 py-0.5 text-xs transition-colors"
                :class="
                  skillsScope === scope
                    ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
                    : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                "
                :data-test="`session-preset-skills-scope-${scope}`"
                @click="skillsScope = scope">
                {{ t(`config.agent.skills.scope_${scope}`) }}
              </button>
            </div>
            <div
              v-if="visibleSkills.length === 0"
              class="px-3 py-1 text-gray-400"
              data-test="session-preset-skills-empty">
              {{ t('config.agent.skills.empty') }}
            </div>
            <label
              v-for="skill in visibleSkills"
              :key="skill.name"
              class="flex items-center gap-1.5 py-0.5 text-gray-600 dark:text-gray-300"
              :title="skill.description || skill.name">
              <Checkbox
                :model-value="sessionEnabledSkills.includes(skill.name)"
                binary
                disabled
                :data-test="`session-preset-skill-${skill.name}`" />
              <span class="font-mono">{{ skill.name }}</span>
            </label>
          </div>
        </TabPanel>
        <!-- LAST tab: the SESSION's extraction overrides. Always present (even
             with no bound preset): experience learning and the two iteration
             ceilings are editable and park until the next turn; flow replay
             compilation is read-only here because it is a global decision. -->
        <TabPanel
          value="advanced"
          :header="t('advanced.tab')"
          data-test="session-preset-advanced-tab">
          <ExtractionSettingsPanel
            scope="session"
            :session-id="sessionId" />
        </TabPanel>
      </TabView>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import ExtractionSettingsPanel from './ExtractionSettingsPanel.vue';
import ThinkingCustomMappingPanel from './ThinkingCustomMappingPanel.vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import type { SessionPresetBinding } from '@/composables/db';
import type { PersonaPresetPayload } from '@/composables/persona-catalog';
import { FOLLOW_TIER_ID } from '~/stores/agent-config';
import { ENV_MODEL_ID } from '~/stores/session-model';
import { logUtil } from '~/utils/log';

const { t, locale } = useI18n();
const route = useRoute();
const agentStore = useAgentConfigStore();

/**
 * The persona files previewed as tabs, each with the i18n key suffix of its tab
 * header (the same `config.tabs.*` labels the preset panel uses).
 */
const PREVIEW_FILES: ReadonlyArray<{ file: string; tabKey: string }> = [
  { file: 'SOUL.md', tabKey: 'soul' },
  { file: 'USER.md', tabKey: 'user' }
];

/** The preview tabs only exist when there is a bound preset to show. */
const visiblePreviewFiles = computed(() => (payload.value ? PREVIEW_FILES : []));

/** The saved presets (shared singleton); built-ins come from the catalogue. */
const { presets } = usePersonaPresets();

const llmProfiles = useLlmProfilesStore();

/** The two Agent models sub-tabs (mirroring the editor). */
const AGENT_MODEL_SCOPES = ['main', 'subagent'] as const;
const modelScope = ref<(typeof AGENT_MODEL_SCOPES)[number]>('main');

/** The session's main-model control (its own store: the toolbar picker's key). */
const sessionModel = useSessionModelStore();

/** Select-options: "follow the environment config" + every env-config profile. */
const mainModelOptions = computed<Array<{ label: string; value: string }>>(() => [
  { label: t('config.agent.models.followEnv'), value: ENV_MODEL_ID },
  ...llmProfiles.listFor('MAIN_LLM').map(profile => ({ label: profile.label, value: profile.id }))
]);

/**
 * Point the session at an env-config profile (or back at the environment
 * config). Writes through the session-model store — the same optimistic +
 * rollback path the toolbar's model switch uses, so the two stay in step and a
 * mid-turn change parks until the next turn.
 * @param profileId Chosen profile id (the env sentinel = follow the env config).
 */
const chooseMainModel = async (profileId: string): Promise<void> => {
  const sid = sessionId.value;
  if (!sid) return;
  const chosen = String(profileId ?? '');
  const profile = chosen && chosen !== ENV_MODEL_ID ? llmProfiles.byId('MAIN_LLM', chosen) : undefined;
  await sessionModel.select(sid, profile ? llmProfiles.toSessionProfile(profile) : null);
};

/** The Main agent row's text: the env default (with its model) or the profile label. */
const mainModelLabel = computed<string>(() => {
  const sid = sessionId.value;
  if (!sid) return t('config.agent.models.followEnv');
  const current = sessionModel.currentId(sid);
  if (current === ENV_MODEL_ID) {
    const envModel = sessionModel.envModel.model;
    return `${t('config.agent.models.followEnv')}${envModel ? ` · ${envModel}` : ''}`;
  }
  return llmProfiles.listFor('MAIN_LLM').find(profile => profile.id === current)?.label ?? current;
});

/** The session's EFFECTIVE skill selection (null = no opinion = every skill). */
const sessionEnabledSkills = computed<string[]>(() => {
  if (!sessionId.value) return [];
  const configured = agentStore.selectedSkills(sessionId.value);
  return configured ?? agentStore.catalog.skills.map(skill => skill.name);
});

/** The two Skills sub-tabs, mirroring the editor. */
const AGENT_SKILL_SCOPES = ['builtin', 'thirdparty'] as const;
const skillsScope = ref<(typeof AGENT_SKILL_SCOPES)[number]>('builtin');

/** The skills of the active sub-tab. */
const visibleSkills = computed(() =>
  skillsScope.value === 'builtin' ? agentStore.skills.builtin : agentStore.skills.thirdParty
);

/** Select options: "follow the role tier" + every env-config profile. */
const roleModelOptions = computed<Array<{ label: string; value: string }>>(() => [
  { label: t('config.agent.subagents.followTier'), value: FOLLOW_TIER_ID },
  ...llmProfiles.listFor('MAIN_LLM').map(profile => ({ label: profile.label, value: profile.id }))
]);

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
  models[role] = profile ? llmProfiles.toSubagentProfile(profile) : null;
  await agentStore.save(sid, { ...current, subagent_models: models });
};

/** The 8K business floor for a role profile's output cap. */
const MIN_ROLE_MAX_OUTPUT_TOKENS = 8192;

/**
 * One role's output cap, defaulting to the 8K floor for legacy profiles.
 * @param role Functional role.
 */
const roleMaxOutput = (role: string): number => {
  const raw = agentStore.configOf(sessionId.value).subagent_models?.[role]?.max_output_tokens;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed >= MIN_ROLE_MAX_OUTPUT_TOKENS ? parsed : MIN_ROLE_MAX_OUTPUT_TOKENS;
};

/**
 * Persist one role profile's output cap (the selected profile is preserved).
 * @param role Functional role.
 * @param event Change event from the numeric input.
 */
const onRoleMaxOutputChange = async (role: string, event: Event): Promise<void> => {
  const sid = sessionId.value;
  if (!sid) return;
  const current = agentStore.configOf(sid);
  const profile = current.subagent_models?.[role];
  if (!profile) return;
  const parsed = Number((event.target as HTMLInputElement).value);
  const next = Number.isFinite(parsed)
    ? Math.max(MIN_ROLE_MAX_OUTPUT_TOKENS, Math.trunc(parsed))
    : MIN_ROLE_MAX_OUTPUT_TOKENS;
  const models = { ...(current.subagent_models ?? {}) };
  models[role] = { ...profile, max_output_tokens: String(next) };
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
 * preset still reads right), or 未选择 ("nothing selected") for a session that has no binding.
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
    // block (Pure / Companion derive theirs from it); loading is idempotent.
    await agentStore.loadCatalog();
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
    // The catalogue backs the tabs; the per-session config is what the three
    // agent tabs display (and Agent models edits) — both are cheap and idempotent.
    void agentStore.loadCatalog();
    if (sessionId.value) {
      void agentStore.hydrate(sessionId.value);
      // The Main agent sub-tab mirrors the session-model control (the toolbar
      // picker's own store), so hydrate that one too.
      void sessionModel.hydrate(sessionId.value);
    }
    void loadBinding();
  },
  { immediate: true }
);
watch(presets, () => {
  void loadBinding();
});
</script>
