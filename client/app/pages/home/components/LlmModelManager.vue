<template>
  <div class="flex flex-col gap-3 rounded-lg border border-gray-100 p-3 dark:border-gray-800">
    <!-- Group header doubles as the collapse toggle; panels start collapsed
         so a long model list does not push the other groups off screen. -->
    <button
      type="button"
      class="flex w-full cursor-pointer items-center gap-2 text-left"
      :aria-expanded="!collapsed"
      :aria-label="collapsed ? t('config.llm.expand') : t('config.llm.collapse')"
      @click="collapsed = !collapsed">
      <i
        :class="['pi text-xs text-gray-400', collapsed ? 'pi-chevron-right' : 'pi-chevron-down']"
        aria-hidden="true"></i>
      <span class="text-xs font-semibold text-gray-500 dark:text-gray-400">{{ groupTitle }}</span>
    </button>

    <div
      v-if="!collapsed"
      class="grid gap-4 md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]">
      <!-- Left column: model list + add button -->
      <div class="flex flex-col gap-2">
        <!-- Saved-model budget: only the stored (non-local) entries count against
             the cap, so the counter sits next to the button it disables. -->
        <div class="flex items-center gap-2">
          <Button
            :label="t('config.llm.add')"
            icon="pi pi-plus"
            size="small"
            severity="secondary"
            outlined
            class="flex-1"
            :disabled="atCapacity"
            :title="atCapacity ? t('config.llm.maxModels', { max: MAX_PROFILES_PER_GROUP }) : ''"
            @click="addModel" />
          <span
            class="shrink-0 font-mono text-xs"
            :class="atCapacity ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'">
            {{ t('config.llm.count', { n: profiles.length, max: MAX_PROFILES_PER_GROUP }) }}
          </span>
        </div>

        <p
          v-if="profiles.length === 0"
          class="m-0 text-xs text-gray-400 dark:text-gray-500">
          {{ t('config.llm.empty') }}
        </p>

        <!-- Built-in local entry stays PINNED above the scroll area; the saved
             profiles below scroll once they outgrow the box. -->
        <LlmProfileRow
          v-if="localEntry"
          :entry="localEntry"
          :selected="localEntry.id === selectedId"
          :active="isActive(localEntry)"
          @select="selectModel(localEntry.id)" />
        <div class="flex max-h-56 flex-col gap-2 overflow-y-auto">
          <LlmProfileRow
            v-for="row in profileRows"
            :key="row.id"
            :entry="row"
            :selected="row.id === selectedId"
            :active="isActive(row)"
            @select="selectModel(row.id)" />
        </div>
      </div>

      <!-- Right column: parameters of the selected model + save/apply -->
      <div
        v-if="selected"
        class="flex flex-col gap-2">
        <div
          v-for="key in paramKeys"
          :key="key"
          class="flex flex-col gap-1">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ key }}</span>
          <InputText
            v-model="draft[key]"
            :disabled="isLocalEntrySelected"
            :placeholder="isLocalEntrySelected ? t('config.llm.localUnused') : ''"
            :class="[
              'w-full font-mono text-xs disabled:opacity-60',
              missingRequired && (key === providerKey || key === nameKey) && !(draft[key] ?? '').trim()
                ? 'border-red-400'
                : ''
            ]"
            autocomplete="off"
            spellcheck="false" />
        </div>

        <div class="mt-1 flex items-center gap-2">
          <!-- The built-in local entry is read-only: it can be applied, never saved. -->
          <Button
            v-if="!isLocalEntrySelected"
            :label="t('config.llm.save')"
            icon="pi pi-save"
            size="small"
            outlined
            @click="saveProfile" />
          <Button
            :label="t('config.llm.apply')"
            icon="pi pi-check"
            size="small"
            @click="applyProfile" />
          <!-- Delete stays in the same action row; the built-in local entry and
               the empty selection have nothing to delete. -->
          <Button
            v-if="!isLocalEntrySelected && selected"
            :label="t('config.llm.delete')"
            icon="pi pi-trash"
            size="small"
            severity="danger"
            text
            class="ml-auto"
            @click="deleteSelected" />
          <span
            v-if="flash"
            :class="[
              'text-xs',
              flashError ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400'
            ]">
            {{ flash }}
          </span>
        </div>
      </div>
      <div
        v-else
        class="flex items-center justify-center text-xs text-gray-400 dark:text-gray-500">
        {{ t('config.llm.empty') }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import LlmProfileRow from './LlmProfileRow.vue';
import { MAX_PROFILES_PER_GROUP } from '~/stores/llm-profiles';

const props = defineProps<{
  /** Env group this panel manages (e.g. `MAIN_LLM`, `TTI`). */
  group: string;
  /** Keys of that group present in `.env` (the editable parameter set). */
  keys: string[];
  /** Live `.env` values for those keys (used to seed new profiles / infer the applied one). */
  values: Record<string, string>;
  /** Group heading shown above the panel. */
  groupTitle?: string;
}>();

const emit = defineEmits<{
  /** Request to apply a profile to `.env`; the parent performs the write and marks it active. */
  (e: 'apply', payload: { id: string; params: Record<string, string> }): void;
}>();

const { t } = useI18n();
/** Global confirmation-dialog service (ConfirmDialog is mounted in app.vue). */
const confirm = useConfirm();
/** Client-side model profiles (persisted, per group). */
const store = useLlmProfilesStore();

/** Collapsed by default; the header toggles it. */
const collapsed = ref(true);

/** Profiles of this group (reactive). */
const profiles = computed(() => store.listFor(props.group));

// Stored data may exceed the cap (older payloads / hand-edited storage):
// trim once on panel setup, by creation time, keeping the oldest entries.
store.trimGroup(props.group);

/**
 * Provider and model-API-name are mandatory: an empty one cannot be saved or
 * applied (the backend cannot build a client from it). The built-in local
 * entry is exempt — it carries no API parameters by design.
 */
const missingRequired = computed<boolean>(() => {
  if (!selected.value || isLocalEntrySelected.value) return false;
  return props.keys.some(
    key => (key === providerKey.value || key === nameKey.value) && !(draft.value[key] ?? '').trim()
  );
});

/** At the per-group cap: the add button is disabled (and `add` refuses). */
const atCapacity = computed(() => profiles.value.length >= MAX_PROFILES_PER_GROUP);

/** Currently selected profile id (the right column edits this one). */
const selectedId = ref<string | null>(profiles.value[0]?.id ?? null);
/** Working copy of the selected profile's parameters. */
const draft = ref<Record<string, string>>({});
/** Transient "saved" confirmation text. */
const flash = ref('');
let flashTimer: ReturnType<typeof setTimeout> | null = null;

/** Synthetic id of the built-in local-model entry (never persisted as a profile). */
const LOCAL_ENTRY_ID = 'builtin:local';

const localFlagKey = computed(() => props.keys.find(k => k.endsWith('_MODEL_LOCAL')));

/** Parameter keys shown as inputs: the local flag never is (it is derived on apply). */
const paramKeys = computed(() => props.keys.filter(k => k !== localFlagKey.value));

/**
 * The built-in "local model" entry: the group's live `.env` parameters with the
 * local flag forced on. Read-only in the UI (disabled inputs, no 保存) but
 * applicable like any other model; absent for groups without a local flag.
 */
const localEntry = computed(() => {
  const key = localFlagKey.value;
  if (!key) return null;
  // The API parameters are UNUSED in local mode (the backend runs its bundled
  // model and never reads them), so the entry shows them empty instead of
  // mirroring the remote values; applying writes ONLY the flag and leaves the
  // group's other keys untouched, so switching back to a cloud model keeps the
  // original configuration.
  const params: Record<string, string> = {};
  for (const k of props.keys) params[k] = k === key ? 'true' : '';
  return { id: LOCAL_ENTRY_ID, label: t('config.llm.localModel'), params };
});

/** Left-column rows: the built-in local entry first, then the saved profiles. */
const listEntries = computed(() => {
  // Rows are labelled by the model's API name (what the backend actually
  // calls); the stored label is only a fallback for entries whose API name is
  // still empty.
  const entries: Array<{ id: string; label: string; params: Record<string, string> }> = [
    ...profiles.value.map(p => ({
      id: p.id,
      label: p.params[nameKey.value] || p.label || t('config.llm.unnamed'),
      params: p.params
    }))
  ];
  if (localEntry.value) entries.unshift(localEntry.value);
  return entries;
});

/** Saved-profile rows (listEntries without the pinned built-in entry). */
const profileRows = computed(() => listEntries.value.filter(e => e.id !== LOCAL_ENTRY_ID));

/** Whether the built-in local entry is the one being viewed. */
const isLocalEntrySelected = computed(() => selectedId.value === LOCAL_ENTRY_ID);

/** The viewed entry: a saved profile, or the built-in local model. */
const selected = computed(() =>
  isLocalEntrySelected.value ? localEntry.value : store.byId(props.group, selectedId.value)
);

/** The provider key of this group (any `*_PROVIDER` variant, e.g. `ITTT_model_PROVIDER`). */
const providerKey = computed(() => props.keys.find(k => k.endsWith('_PROVIDER')) ?? '');
/** The model-name key of this group (`*_API_NAME` wins over `*_NAME`). */
const nameKey = computed(
  () => props.keys.find(k => k.endsWith('_API_NAME')) ?? props.keys.find(k => k.endsWith('_NAME')) ?? ''
);

/**
 * True when the profile is the one applied to `.env` for this group.
 * @param profile
 * @param profile.id
 * @param profile.params
 */
const isActive = (profile: { id: string; params: Record<string, string> }): boolean => {
  if (store.activeIdFor(props.group) === profile.id) return true;
  // Built-in local entry: with no marker, it is the active one when `.env`
  // already runs this group in local mode.
  if (profile.id === LOCAL_ENTRY_ID) {
    const key = localFlagKey.value;
    return store.activeIdFor(props.group) === null && !!key && (props.values[key] ?? '').toLowerCase() === 'true';
  }
  // No marker yet (fresh browser): fall back to comparing with the live .env,
  // so the dot is honest instead of absent until the first apply.
  if (store.activeIdFor(props.group) !== null) return false;
  const name = profile.params[nameKey.value] ?? '';
  const provider = profile.params[providerKey.value] ?? '';
  return !!name && name === props.values[nameKey.value] && (!provider || provider === props.values[providerKey.value]);
};

/** Load the selected profile's parameters into the draft (missing keys fall back to .env). */
const syncDraft = () => {
  const profile = selected.value;
  const next: Record<string, string> = {};
  for (const key of props.keys) next[key] = profile?.params[key] ?? props.values[key] ?? '';
  draft.value = next;
};
// Keep the built-in entry's read-only display in sync with the live `.env`.
watch(
  () => props.values,
  () => {
    if (isLocalEntrySelected.value) syncDraft();
  },
  { deep: true }
);

watch(selectedId, syncDraft);
watch(() => props.keys.join('|'), syncDraft);
syncDraft();

/**
 * Show a transient inline confirmation.
 * @param text
 */
const flashError = ref(false);
/**
 * Show a transient inline confirmation / error.
 * @param text
 * @param isError Render in red and keep it a little longer.
 */
const showFlash = (text: string, isError = false) => {
  flash.value = text;
  flashError.value = isError;
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(
    () => {
      flash.value = '';
      flashError.value = false;
    },
    isError ? 3000 : 2000
  );
};

/** Create a profile seeded from the live `.env` values and select it. */
const addModel = () => {
  const params: Record<string, string> = {};
  for (const key of props.keys) params[key] = props.values[key] ?? '';
  const label = params[nameKey.value] || t('config.llm.unnamed');
  const id = store.add(props.group, label, params);
  if (id === null) return; // cap reached between render and click
  selectedId.value = id;
  showFlash('');
};

/**
 * Select a profile for editing.
 * @param id
 */
const selectModel = (id: string) => {
  selectedId.value = id;
};

/** Persist the edited parameters into the selected profile (client-side only). */
const saveProfile = () => {
  if (!selected.value || isLocalEntrySelected.value) return; // built-in entry is not saveable
  if (missingRequired.value) {
    showFlash(t('config.llm.requiredMissing'), true);
    return;
  }
  const params: Record<string, string> = { ...draft.value };
  store.update(props.group, selected.value!.id, {
    label: params[nameKey.value] || selected.value.label,
    params
  });
  showFlash(t('config.llm.saved'));
};

/** Ask the parent to write the draft into `.env` (parent marks it active on success). */
/**
 * Apply payload for an entry: the built-in local entry writes only the flag on,
 * a saved profile writes its parameters with the flag off.
 * @param entry
 * @param entry.id
 * @param entry.params
 */
const payloadFor = (entry: { id: string; params: Record<string, string> }): Record<string, string> => {
  const flagKey = localFlagKey.value;
  if (entry.id === LOCAL_ENTRY_ID) return flagKey ? { [flagKey]: 'true' } : {};
  const params: Record<string, string> = { ...entry.params };
  if (flagKey) params[flagKey] = 'false';
  return params;
};

/**
 * Delete the selected saved profile. When entries remain, the PREVIOUS one is
 * applied automatically (falling back to the first survivor when the deleted
 * row was the topmost), so the group always keeps an applied model.
 */
const performDelete = () => {
  const entry = selected.value;
  if (!entry || isLocalEntrySelected.value) return;
  const order = listEntries.value;
  const index = order.findIndex(e => e.id === entry.id);
  const fallback = order[index - 1] ?? order[index + 1] ?? null;
  store.remove(props.group, entry.id);
  if (fallback) {
    selectedId.value = fallback.id;
    emit('apply', { id: fallback.id, params: payloadFor(fallback) });
    return;
  }
  // No saved profile left: fall back to the built-in local model when the
  // group has one, so the group is never left without an applied model.
  if (localEntry.value) {
    selectedId.value = localEntry.value.id;
    emit('apply', { id: localEntry.value.id, params: payloadFor(localEntry.value) });
    return;
  }
  selectedId.value = null;
};

/** Delete asks for confirmation first (global ConfirmDialog via useConfirm). */
const deleteSelected = () => {
  const entry = selected.value;
  if (!entry || isLocalEntrySelected.value) return;
  // Ask with the row's DISPLAY label (the model API name), matching the list.
  const displayName = profileRows.value.find(row => row.id === entry.id)?.label ?? entry.label;
  confirm.require({
    header: t('common.confirmDelete'),
    message: t('config.llm.deleteConfirm', { name: displayName }),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: performDelete
  });
};

const applyProfile = () => {
  if (!selected.value) return;
  if (missingRequired.value) {
    showFlash(t('config.llm.requiredMissing'), true);
    return;
  }
  const flagKey = localFlagKey.value;
  // The built-in local entry needs NO API parameters (the backend ignores them
  // in local mode): apply writes only the flag, leaving the group's other keys
  // in `.env` untouched. A saved profile applies what is on screen (its draft)
  // with the flag turned off — the applied entry decides the flag, never a
  // typed value.
  const params: Record<string, string> = isLocalEntrySelected.value
    ? payloadFor(selected.value)
    : { ...draft.value, ...(flagKey ? { [flagKey]: 'false' } : {}) };
  emit('apply', { id: selected.value.id, params });
};

onBeforeUnmount(() => {
  if (flashTimer) clearTimeout(flashTimer);
});
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "llm": {
        "add": "添加模型",
        "save": "保存",
        "apply": "应用",
        "saved": "已保存",
        "applied": "已应用到 .env",
        "empty": "暂无模型，点击“添加模型”创建",
        "unnamed": "未命名模型"
      }
    }
  },
  "en": {
    "config": {
      "llm": {
        "add": "Add model",
        "save": "Save",
        "apply": "Apply",
        "saved": "Saved",
        "applied": "Applied to .env",
        "empty": "No models yet — click “Add model”",
        "unnamed": "Unnamed model"
      }
    }
  },
  "ja": {
    "config": {
      "llm": {
        "add": "モデルを追加",
        "save": "保存",
        "apply": "適用",
        "saved": "保存しました",
        "applied": ".env に適用済み",
        "empty": "モデルがありません。「モデルを追加」をクリック",
        "unnamed": "名称未設定のモデル"
      }
    }
  },
  "ko": {
    "config": {
      "llm": {
        "add": "모델 추가",
        "save": "저장",
        "apply": "적용",
        "saved": "저장됨",
        "applied": ".env에 적용됨",
        "empty": "모델이 없습니다. “모델 추가”를 클릭하세요",
        "unnamed": "이름 없는 모델"
      }
    }
  }
}
</i18n>
