<template>
  <div
    class="ml-auto flex items-center gap-2 pl-2"
    :class="{ 'opacity-50 pointer-events-none': streaming }">
    <span class="hidden md:inline text-xs text-gray-500 dark:text-gray-400 select-none">
      {{ t('thinkingToggle.label') }}
    </span>

    <!-- Switch models: on/off ToggleSwitch -->
    <ToggleSwitch
      v-if="store.mode === 'on_off'"
      v-model="boolChecked"
      @value-change="handleSwitch">
      <template #handle="{ checked: on }">
        <i :class="['!text-xs pi pi-bolt', on ? '!text-theme-main' : '!text-gray-400']"></i>
      </template>
    </ToggleSwitch>

    <!-- Always-think models: 低 / 高 / 最高 level selector -->
    <div
      v-else
      class="flex items-center gap-1">
      <Button
        v-for="lvl in levels"
        :key="lvl"
        :label="t(`thinkingToggle.${lvl}`)"
        size="small"
        :variant="currentLevel === lvl ? 'primary' : 'text'"
        @click="handleLevel(lvl)" />
    </div>

    <!-- Mobile fallback: single button cycling the next state -->
    <button
      type="button"
      class="block sm:hidden cursor-pointer"
      :aria-label="t('thinkingToggle.a11y')"
      :title="t('thinkingToggle.label')"
      @click="cycleMobile">
      <i :class="['pi pi-bolt text-sm', active ? 'text-theme-main' : 'text-gray-400']"></i>
    </button>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

const props = defineProps<{ sessionId: string; streaming?: boolean }>();

const { t } = useI18n();
/** Per-session thinking control store (hydrated from the backend). */
const store = useThinkingStore();

const levels: Array<'low' | 'high' | 'max'> = ['low', 'high', 'max'];

/** Local mirror of the store value (kept in sync for the switch binding). */
const boolChecked = ref(false);
const currentLevel = computed(() => {
  const v = store.current(props.sessionId);
  return v === 'low' || v === 'high' || v === 'max' ? v : 'high';
});
const active = computed(() => {
  if (store.mode === 'levels') return true; // always-think models are always on
  return store.current(props.sessionId) === true;
});

watch(
  () => store.current(props.sessionId),
  value => {
    boolChecked.value = value === true;
  },
  { immediate: true }
);

/** Hydrate on first mount and whenever the session changes. */
watch(
  () => props.sessionId,
  sessionId => {
    if (sessionId) store.hydrate(sessionId);
  },
  { immediate: true }
);

/**
 * Push a switch change to the store (which persists to the backend).
 * Blocked while the session is streaming — the model variant must never
 * change mid-turn.
 * @param value ToggleSwitch emits a boolean.
 */
const handleSwitch = (value: string | boolean) => {
  if (props.streaming) return;
  const enabled = value === true || value === 'true';
  boolChecked.value = enabled;
  store.setValue(props.sessionId, enabled);
};

/**
 * Select an explicit thinking level (levels mode).
 * @param lvl
 */
const handleLevel = (lvl: 'low' | 'high' | 'max') => {
  if (props.streaming) return;
  store.setValue(props.sessionId, lvl);
};

/**
 * Mobile: cycle on_off → off/on; levels → low → high → max → low.
 */
const cycleMobile = () => {
  if (props.streaming) return;
  if (store.mode === 'levels') {
    const next = currentLevel.value === 'low' ? 'high' : currentLevel.value === 'high' ? 'max' : 'low';
    store.setValue(props.sessionId, next);
    return;
  }
  store.setValue(props.sessionId, store.current(props.sessionId) !== true);
};
</script>

<i18n lang="json">
{
  "zh": {
    "thinkingToggle": {
      "label": "思考",
      "a11y": "控制模型思考能力",
      "low": "低",
      "high": "高",
      "max": "最高"
    }
  },
  "en": {
    "thinkingToggle": {
      "label": "Thinking",
      "a11y": "Toggle model thinking",
      "low": "Low",
      "high": "High",
      "max": "Max"
    }
  },
  "ja": {
    "thinkingToggle": {
      "label": "思考",
      "a11y": "モデルの思考モードを切り替え",
      "low": "低",
      "high": "高",
      "max": "最大"
    }
  },
  "ko": {
    "thinkingToggle": {
      "label": "생각",
      "a11y": "모델思考 모드 전환",
      "low": "낮음",
      "high": "높음",
      "max": "최대"
    }
  }
}
</i18n>
