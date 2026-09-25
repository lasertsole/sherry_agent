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
          v-if="store.profiles.length === 0"
          class="m-0 text-xs text-gray-400 dark:text-gray-500">
          {{ t('config.llm.empty') }}
        </p>

        <div
          v-for="profile in store.profiles"
          :key="profile.id"
          role="button"
          tabindex="0"
          :class="[
            'flex items-center gap-2 rounded-md border px-2 py-1.5 text-xs cursor-pointer transition-colors',
            profile.id === selectedId
              ? 'border-theme-main bg-blue-50 dark:bg-blue-900/20'
              : 'border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/40'
          ]"
          @click="selectModel(profile.id)"
          @keydown.enter.prevent="selectModel(profile.id)"
          @keydown.space.prevent="selectModel(profile.id)">
          <span
            class="h-2 w-2 shrink-0 rounded-full"
            :class="isActive(profile) ? 'bg-emerald-500' : 'bg-transparent'"
            :title="isActive(profile) ? t('config.llm.applied') : ''"
            :data-active="isActive(profile) ? 'true' : 'false'"></span>
          <span class="min-w-0 flex-1 truncate">{{ profile.label }}</span>
        </div>
      </div>

      <!-- Right column: parameters of the selected model + save/apply -->
      <div
        v-if="selected"
        class="flex flex-col gap-2">
        <div
          v-for="key in props.keys"
          :key="key"
          class="flex flex-col gap-1">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ key }}</span>
          <InputText
            v-model="draft[key]"
            class="w-full font-mono text-xs"
            autocomplete="off"
            spellcheck="false" />
        </div>

        <div class="mt-1 flex items-center gap-2">
          <Button
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
        <p class="m-0 text-xs text-gray-400 dark:text-gray-500">
          {{ t('config.llm.hint') }}
        </p>
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
  /** MAIN_LLM_* keys present in `.env` (the editable parameter set). */
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
/** Client-side model profiles (persisted). */
const store = useLlmProfilesStore();

/** Currently selected profile id (the right column edits this one). */
const selectedId = ref<string | null>(store.profiles[0]?.id ?? null);
/** Working copy of the selected profile's parameters. */
const draft = ref<Record<string, string>>({});
/** Transient "saved" confirmation text. */
const flash = ref('');
let flashTimer: ReturnType<typeof setTimeout> | null = null;

const selected = computed(() => store.byId(selectedId.value));

/**
 * True when the profile is the one applied to `.env`.
 * @param profile
 * @param profile.id
 * @param profile.params
 */
const isActive = (profile: { id: string; params: Record<string, string> }): boolean => {
  if (store.activeId === profile.id) return true;
  // No marker yet (fresh browser): fall back to comparing with the live .env,
  // so the dot is honest instead of absent until the first apply.
  if (store.activeId !== null) return false;
  const name = profile.params.MAIN_LLM_NAME ?? '';
  const provider = profile.params.MAIN_LLM_PROVIDER ?? '';
  return !!name && name === props.values.MAIN_LLM_NAME && (!provider || provider === props.values.MAIN_LLM_PROVIDER);
};

/** Load the selected profile's parameters into the draft (missing keys fall back to .env). */
const syncDraft = () => {
  const profile = selected.value;
  const next: Record<string, string> = {};
  for (const key of props.keys) next[key] = profile?.params[key] ?? props.values[key] ?? '';
  draft.value = next;
};

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
  const label = params.MAIN_LLM_NAME || t('config.llm.unnamed');
  const id = store.add(label, params);
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
  if (!selected.value) return;
  const params: Record<string, string> = { ...draft.value };
  store.update(selected.value.id, { label: params.MAIN_LLM_NAME || selected.value.label, params });
  showFlash(t('config.llm.saved'));
};

/** Ask the parent to write the draft into `.env` (parent marks it active on success). */
const applyProfile = () => {
  if (!selected.value) return;
  emit('apply', { id: selected.value.id, params: { ...draft.value } });
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
