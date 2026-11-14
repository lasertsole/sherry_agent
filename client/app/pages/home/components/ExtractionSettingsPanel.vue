<template>
  <!-- The 预设角色 → 高级 tab body: one shared component for both entries.
       scope="global" edits the app-wide sherry.jsonc values (applies at once);
       scope="session" edits this session's overrides (parked until the next
       turn), with flow replay compilation read-only because it is a global
       decision. -->
  <div
    class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto text-xs"
    data-test="extraction-settings-panel">
    <div
      v-if="loading"
      class="flex flex-1 items-center justify-center py-6">
      <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
    </div>
    <p
      v-else-if="loadError"
      class="m-0 text-red-500"
      data-test="extraction-load-error">
      {{ loadError }}
    </p>
    <template v-else>
      <div class="flex items-start gap-1 text-gray-400">
        <i class="pi pi-info-circle mt-0.5 shrink-0" />
        <span data-test="extraction-scope-hint">
          {{ scope === 'session' ? t('advanced.sessionHint') : t('advanced.globalHint') }}
        </span>
      </div>

      <!-- Experience learning -->
      <div
        class="flex items-start justify-between gap-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
        <div class="flex min-w-0 flex-col gap-1">
          <span class="text-sm font-medium text-theme-main">{{ t('advanced.skillExtraction.name') }}</span>
          <span class="text-gray-400">{{ t('advanced.skillExtraction.description') }}</span>
          <span
            v-if="scope === 'session'"
            class="text-[11px] text-gray-400">
            {{ overridden('skill_extraction_enabled') ? t('advanced.overridden') : t('advanced.inherited') }}
          </span>
        </div>
        <ToggleSwitch
          v-model="draft.skill"
          :disabled="saving"
          :aria-label="t('advanced.skillExtraction.name')"
          data-test="extraction-skill-switch" />
      </div>

      <!-- Flow replay compilation: global-only in a session; parented to the
           experience switch in both scopes. -->
      <div
        class="flex items-start justify-between gap-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
        <div class="flex min-w-0 flex-col gap-1">
          <span class="text-sm font-medium text-theme-main">{{ t('advanced.flowExtraction.name') }}</span>
          <span class="text-gray-400">{{ t('advanced.flowExtraction.description') }}</span>
          <span
            v-if="scope === 'session'"
            class="flex items-center gap-1 text-[11px] text-gray-400"
            data-test="extraction-flow-global-only">
            <i class="pi pi-lock text-[10px]" />
            {{ t('advanced.flowExtraction.globalOnly') }}
          </span>
          <span
            v-else-if="!draft.skill"
            class="text-[11px] text-orange-500"
            data-test="extraction-flow-requires-skill">
            {{ t('advanced.flowExtraction.requiresSkill') }}
          </span>
        </div>
        <ToggleSwitch
          :model-value="scope === 'session' ? effectiveFlow : draft.flow"
          :disabled="scope === 'session' || !draft.skill || saving"
          :aria-label="t('advanced.flowExtraction.name')"
          data-test="extraction-flow-switch" />
      </div>

      <!-- Iteration ceilings -->
      <div
        v-for="field in BUDGET_FIELDS"
        :key="field.key"
        class="flex items-start justify-between gap-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
        <div class="flex min-w-0 flex-col gap-1">
          <span class="text-sm font-medium text-theme-main">{{ t(`advanced.${field.i18n}.name`) }}</span>
          <span class="text-gray-400">{{ t(`advanced.${field.i18n}.description`) }}</span>
          <span
            v-if="scope === 'session'"
            class="text-[11px] text-gray-400">
            {{
              overridden(field.key)
                ? t('advanced.overridden')
                : t('advanced.inheritedWithGlobal', { value: globalValue(field.key) })
            }}
          </span>
        </div>
        <input
          v-model.number="draft[field.draft]"
          type="number"
          :min="bounds.min_iterations"
          :max="bounds.max_iterations"
          step="1"
          :disabled="saving"
          class="w-24 shrink-0 rounded border border-solid border-gray-light bg-transparent px-1.5 py-0.5 text-right text-xs text-gray-600 outline-none focus:border-theme-main dark:border-[#555] dark:text-gray-300"
          :data-test="`extraction-${field.draft}-input`" />
      </div>

      <p
        v-if="validationError"
        class="m-0 text-red-500"
        data-test="extraction-validation-error">
        {{ validationError }}
      </p>

      <div class="flex justify-end gap-2">
        <Button
          v-if="scope === 'session'"
          :label="t('advanced.resetToGlobal')"
          icon="pi pi-undo"
          severity="secondary"
          size="small"
          outlined
          :disabled="saving || !hasOverrides"
          data-test="extraction-reset"
          @click="resetToGlobal" />
        <Button
          :label="t('advanced.save')"
          icon="pi pi-save"
          size="small"
          :loading="saving"
          :disabled="saving || !!validationError"
          data-test="extraction-save"
          @click="save" />
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type {
  ExtractionBounds,
  ExtractionOverrides,
  ExtractionState,
  ExtractionValues
} from '~/composables/bridge/extraction';
// Stable module specifiers so tests can vi.mock the bridge; the unimport
// injection is compile-time and leaves bare symbols unmockable.
/* eslint-disable @typescript-eslint/no-restricted-imports */
import {
  fetchExtractionState,
  fetchGlobalExtraction,
  saveGlobalExtraction,
  saveSessionExtraction
} from '~/composables/bridge/extraction';
/* eslint-enable @typescript-eslint/no-restricted-imports */
import { logUtil } from '~/utils/log';

const props = defineProps<{
  /** Which entry rendered this tab: the app-wide settings or the session's. */
  scope: 'global' | 'session';
  /** The session whose overrides are edited (required for scope="session"). */
  sessionId?: string;
}>();

const { t } = useI18n();

/** The two iteration-ceiling rows (field key ↔ i18n suffix ↔ local draft key). */
const BUDGET_FIELDS = [
  { key: 'iteration_budget_main', i18n: 'iterationBudgetMain', draft: 'main' },
  { key: 'iteration_budget_sub', i18n: 'iterationBudgetSub', draft: 'sub' }
] as const;

/** The draft the inputs edit (numbers may briefly hold an empty string). */
interface Draft {
  skill: boolean;
  flow: boolean;
  main: number | string;
  sub: number | string;
}

const loading = ref(true);
const saving = ref(false);
const loadError = ref('');
const state = ref<ExtractionState | null>(null);
const draft = ref<Draft>({ skill: true, flow: true, main: 180, sub: 90 });

/** The effective flow value a session shows read-only (parent + global applied). */
const effectiveFlow = computed<boolean>(() => state.value?.effective.flow_extraction_enabled === true);

const bounds = computed<ExtractionBounds>(() => state.value?.bounds ?? { min_iterations: 1, max_iterations: 1000 });

/**
 * The global value a session-scoped field inherits (for the inline hint).
 * @param key
 */
const globalValue = (key: string): number => {
  const values = state.value?.global;
  if (!values) return 0;
  if (key === 'iteration_budget_main') return values.iteration_budget_main;
  if (key === 'iteration_budget_sub') return values.iteration_budget_sub;
  return 0;
};

/**
 * Whether this session explicitly overrides *key* (the "overridden" chip).
 * @param key
 */
const overridden = (key: string): boolean => {
  if (props.scope !== 'session' || !state.value) return false;
  const overrides = state.value.session as Record<string, unknown>;
  return Object.prototype.hasOwnProperty.call(overrides, key);
};

/** True when the session carries at least one override (enables Reset). */
const hasOverrides = computed<boolean>(() => {
  const overrides = state.value?.session ?? {};
  return Object.keys(overrides).length > 0;
});

/** Client-side validation mirroring the backend sanitizer (positive ints, sub ≤ main). */
const validationError = computed<string>(() => {
  const main = Number(draft.value.main);
  const sub = Number(draft.value.sub);
  const { min_iterations: min, max_iterations: max } = bounds.value;
  for (const value of [main, sub]) {
    if (!Number.isInteger(value) || value < min || value > max) {
      return t('advanced.invalidBudget', { min, max });
    }
  }
  if (sub > main) return t('advanced.subAboveMain');
  return '';
});

/**
 * Fill the draft from the loaded state.
 * @param values
 */
const hydrateDraft = (values: ExtractionValues): void => {
  draft.value = {
    skill: values.skill_extraction_enabled,
    flow: values.flow_extraction_enabled !== false,
    main: values.iteration_budget_main,
    sub: values.iteration_budget_sub
  };
};

/** Load the state for this scope (session id changes re-load). */
const load = async (): Promise<void> => {
  loading.value = true;
  loadError.value = '';
  try {
    if (props.scope === 'session' && props.sessionId) {
      const loaded = await fetchExtractionState(props.sessionId);
      state.value = loaded;
      hydrateDraft(loaded.effective);
    } else if (props.scope === 'session') {
      // No session on screen: never fall through to the GLOBAL editor (that
      // would let a session panel rewrite the app-wide values).
      loadError.value = t('advanced.loadFailed');
    } else {
      const loaded = await fetchGlobalExtraction();
      state.value = {
        global: loaded.global,
        session: {},
        effective: loaded.global,
        pending: false,
        defaults: loaded.defaults,
        bounds: loaded.bounds
      };
      hydrateDraft(loaded.global);
    }
  } catch (e) {
    logUtil.e('[ExtractionSettingsPanel] Failed to load extraction settings:', e);
    loadError.value = t('advanced.loadFailed');
  } finally {
    loading.value = false;
  }
};

/**
 * Session write payload: ONLY the fields that differ from the global values, so
 * an untouched field keeps inheriting later global changes instead of being
 * pinned to the value it showed today.
 */
const sessionOverrides = (): ExtractionOverrides => {
  const globalValues = state.value?.global;
  const overrides: ExtractionOverrides = {};
  if (!globalValues) return overrides;
  if (draft.value.skill !== globalValues.skill_extraction_enabled) {
    overrides.skill_extraction_enabled = draft.value.skill;
  }
  if (Number(draft.value.main) !== globalValues.iteration_budget_main) {
    overrides.iteration_budget_main = Number(draft.value.main);
  }
  if (Number(draft.value.sub) !== globalValues.iteration_budget_sub) {
    overrides.iteration_budget_sub = Number(draft.value.sub);
  }
  return overrides;
};

/** Persist the draft (global: immediately; session: live or parked). */
const save = async (): Promise<void> => {
  if (validationError.value || saving.value) return;
  saving.value = true;
  try {
    if (props.scope === 'session' && props.sessionId) {
      const result = await saveSessionExtraction(props.sessionId, sessionOverrides());
      if (state.value) {
        state.value = {
          ...state.value,
          session: result.overrides,
          effective: result.effective,
          pending: result.pending
        };
      }
      hydrateDraft(result.effective);
      toastSuccess(result.pending ? t('advanced.savedPending') : t('advanced.saved'));
    } else {
      const values = await saveGlobalExtraction({
        skill_extraction_enabled: draft.value.skill,
        flow_extraction_enabled: draft.value.skill ? draft.value.flow : false,
        iteration_budget_main: Number(draft.value.main),
        iteration_budget_sub: Number(draft.value.sub)
      });
      if (state.value) {
        state.value = {
          ...state.value,
          global: values,
          effective: values
        };
      }
      hydrateDraft(values);
      toastSuccess(t('advanced.saved'));
    }
  } catch (e) {
    logUtil.e('[ExtractionSettingsPanel] Failed to save extraction settings:', e);
    toastError(t('advanced.saveFailed'));
  } finally {
    saving.value = false;
  }
};

/** Clear every session override back to the global values. */
const resetToGlobal = async (): Promise<void> => {
  if (!props.sessionId || saving.value) return;
  saving.value = true;
  try {
    const result = await saveSessionExtraction(props.sessionId, {});
    if (state.value) {
      state.value = {
        ...state.value,
        session: result.overrides,
        effective: result.effective,
        pending: result.pending
      };
    }
    hydrateDraft(result.effective);
    toastSuccess(result.pending ? t('advanced.savedPending') : t('advanced.saved'));
  } catch (e) {
    logUtil.e('[ExtractionSettingsPanel] Failed to reset extraction overrides:', e);
    toastError(t('advanced.saveFailed'));
  } finally {
    saving.value = false;
  }
};

watch(
  () => [props.scope, props.sessionId] as const,
  () => {
    void load();
  }
);

onMounted(() => {
  void load();
});
</script>
