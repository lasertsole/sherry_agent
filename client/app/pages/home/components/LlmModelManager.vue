<template>
  <div class="flex flex-col gap-3 rounded-lg border border-gray-100 p-3 dark:border-gray-800">
    <p class="m-0 text-xs font-semibold text-gray-500 dark:text-gray-400">
      {{ groupTitle }}
    </p>

    <div class="grid gap-4 md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]">
      <!-- Left column: model list + add button -->
      <div class="flex flex-col gap-2">
        <Button
          :label="t('config.llm.add')"
          icon="pi pi-plus"
          size="small"
          severity="secondary"
          outlined
          class="w-full"
          @click="addModel" />

        <p
          v-if="listEntries.length === 0"
          class="m-0 text-xs text-gray-400 dark:text-gray-500">
          {{ t('config.llm.empty') }}
        </p>

        <!-- `listEntries` = the built-in local-model entry pinned above the user
             profiles, then the saved profiles themselves. -->
        <div
          v-for="entry in listEntries"
          :key="entry.id"
          role="button"
          tabindex="0"
          :class="[
            'flex items-center gap-2 rounded-md border px-2 py-1.5 text-xs cursor-pointer transition-colors',
            entry.id === selectedId
              ? 'border-theme-main bg-blue-50 dark:bg-blue-900/20'
              : 'border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/40'
          ]"
          @click="selectModel(entry.id)"
          @keydown.enter.prevent="selectModel(entry.id)"
          @keydown.space.prevent="selectModel(entry.id)">
          <span
            class="h-2 w-2 shrink-0 rounded-full"
            :class="isActive(entry) ? 'bg-emerald-500' : 'bg-transparent'"
            :title="isActive(entry) ? t('config.llm.applied') : ''"
            :data-active="isActive(entry) ? 'true' : 'false'"></span>
          <span class="min-w-0 flex-1 truncate">{{ entry.label }}</span>
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
            class="w-full font-mono text-xs disabled:opacity-60"
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
          <span
            v-if="flash"
            class="text-xs text-emerald-600 dark:text-emerald-400">
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
/** Client-side model profiles (persisted, per group). */
const store = useLlmProfilesStore();

/** Profiles of this group (reactive). */
const profiles = computed(() => store.listFor(props.group));

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
  const entries: Array<{ id: string; label: string; params: Record<string, string> }> = [
    ...profiles.value.map(p => ({ id: p.id, label: p.label, params: p.params }))
  ];
  if (localEntry.value) entries.unshift(localEntry.value);
  return entries;
});

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
const showFlash = (text: string) => {
  flash.value = text;
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => (flash.value = ''), 2000);
};

/** Create a profile seeded from the live `.env` values and select it. */
const addModel = () => {
  const params: Record<string, string> = {};
  for (const key of props.keys) params[key] = props.values[key] ?? '';
  const label = params[nameKey.value] || t('config.llm.unnamed');
  const id = store.add(props.group, label, params);
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
  const params: Record<string, string> = { ...draft.value };
  store.update(props.group, selected.value!.id, {
    label: params[nameKey.value] || selected.value.label,
    params
  });
  showFlash(t('config.llm.saved'));
};

/** Ask the parent to write the draft into `.env` (parent marks it active on success). */
const applyProfile = () => {
  if (!selected.value) return;
  const flagKey = localFlagKey.value;
  // The built-in local entry needs NO API parameters (the backend ignores them
  // in local mode): apply writes only the flag, leaving the group's other keys
  // in `.env` untouched. A saved profile carries its parameters and turns the
  // flag off — the applied entry decides the flag, it is never typed.
  const params: Record<string, string> = isLocalEntrySelected.value
    ? flagKey
      ? { [flagKey]: 'true' }
      : {}
    : flagKey
      ? { ...draft.value, [flagKey]: 'false' }
      : { ...draft.value };
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
        "unnamed": "未命名模型",
        "models": "模型列表",
        "hint": "“保存”仅更新本列表；“应用”会写入 .env 并在左侧标记绿点。"
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
        "unnamed": "Unnamed model",
        "models": "Models",
        "hint": "“Save” updates this list only; “Apply” writes .env and marks the entry with a green dot."
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
        "unnamed": "名称未設定のモデル",
        "models": "モデル一覧",
        "hint": "「保存」はこの一覧のみ更新します。「適用」は .env に書き込み、左側に緑のドットを付けます。"
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
        "unnamed": "이름 없는 모델",
        "models": "모델 목록",
        "hint": "“저장”은 이 목록만 갱신합니다. “적용”은 .env에 기록하고 왼쪽에 초록 점을 표시합니다."
      }
    }
  }
}
</i18n>
