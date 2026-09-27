<template>
  <div class="ml-auto flex items-center gap-2 pl-2">
    <span class="hidden md:inline text-xs text-gray-500 dark:text-gray-400 select-none">
      {{ t('thinkingToggle.label') }}
    </span>

    <!-- Collapsed picker for both modes: the trigger shows only the current
         selection (开启/关闭, or 低/高/最高); clicking opens the list. Switching
         is allowed at any moment — a mid-turn choice is parked (clock icon) and
         lands on the next turn. -->
    <Button
      variant="text"
      size="small"
      :label="triggerLabel"
      icon="pi pi-angle-down"
      icon-pos="right"
      :aria-label="t('thinkingToggle.a11y')"
      @click="toggleMenu" />
    <i
      v-if="pending"
      class="pi pi-clock text-xs text-theme-main"
      :title="t('thinkingToggle.pending')"
      :aria-label="t('thinkingToggle.pending')"></i>
    <Menu
      ref="thinkMenu"
      :model="menuItems"
      popup />

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

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
/** Per-session thinking control store (hydrated from the backend). */
const store = useThinkingStore();

const levels: Array<'low' | 'high' | 'max'> = ['low', 'high', 'max'];

/** Popup list of the current mode's options, opened by the collapsed trigger. */
const thinkMenu = ref();

/** Current value of the session's thinking control (bool for on_off, level for levels). */
const currentValue = computed(() => store.current(props.sessionId));
const currentLevel = computed(() =>
  currentValue.value === 'low' || currentValue.value === 'high' || currentValue.value === 'max'
    ? currentValue.value
    : 'high'
);
const thinkingOn = computed(() => currentValue.value === true);

/**
 * Trigger text: the selected option only (the list stays hidden until clicked),
 * mirroring the level picker for switch-style models.
 */
const triggerLabel = computed(() =>
  store.mode === 'levels'
    ? t(`thinkingToggle.${currentLevel.value}`)
    : t(`thinkingToggle.${thinkingOn.value ? 'on' : 'off'}`)
);

/** Menu items for the active mode; the current option carries a check marker. */
const menuItems = computed(() => {
  if (store.mode === 'levels') {
    return levels.map(lvl => ({
      label: t(`thinkingToggle.${lvl}`),
      icon: currentLevel.value === lvl ? 'pi pi-check' : undefined,
      command: () => handleLevel(lvl)
    }));
  }
  return ([true, false] as const).map(enabled => ({
    label: t(`thinkingToggle.${enabled ? 'on' : 'off'}`),
    icon: thinkingOn.value === enabled ? 'pi pi-check' : undefined,
    command: () => handleOnOff(enabled)
  }));
});

/**
 * Whether the current choice is parked: it was made while a turn was running
 * and lands on the next turn (the running turn keeps its variant).
 */
const pending = computed(() => store.isPending(props.sessionId));

const active = computed(() => {
  if (store.mode === 'levels') return true; // always-think models are always on
  return thinkingOn.value;
});

/** Hydrate on first mount and whenever the session changes. */
watch(
  () => props.sessionId,
  sessionId => {
    if (sessionId) store.hydrate(sessionId);
  },
  { immediate: true }
);

/**
 * Enable/disable thinking (switch-style models). Allowed at any moment: a
 * mid-turn switch is parked by the backend and applies next turn.
 * @param enabled
 */
const handleOnOff = (enabled: boolean) => {
  store.setValue(props.sessionId, enabled);
};

/**
 * Select an explicit thinking level (levels mode).
 * @param lvl
 */
const handleLevel = (lvl: 'low' | 'high' | 'max') => {
  store.setValue(props.sessionId, lvl);
};

/**
 * Open the options popup.
 * @param event
 */
const toggleMenu = (event: Event) => {
  thinkMenu.value?.toggle(event);
};

/**
 * Mobile: cycle on_off → off/on; levels → low → high → max → low.
 */
const cycleMobile = () => {
  if (store.mode === 'levels') {
    const next = currentLevel.value === 'low' ? 'high' : currentLevel.value === 'high' ? 'max' : 'low';
    store.setValue(props.sessionId, next);
    return;
  }
  store.setValue(props.sessionId, !thinkingOn.value);
};
</script>

<i18n lang="json">
{
  "zh": {
    "thinkingToggle": {
      "label": "思考",
      "a11y": "控制模型思考能力",
      "pending": "下一轮生效",
      "low": "低",
      "high": "高",
      "max": "最高",
      "on": "开启",
      "off": "关闭"
    }
  },
  "en": {
    "thinkingToggle": {
      "label": "Thinking",
      "a11y": "Toggle model thinking",
      "pending": "Applies next turn",
      "low": "Low",
      "high": "High",
      "max": "Max",
      "on": "On",
      "off": "Off"
    }
  },
  "ja": {
    "thinkingToggle": {
      "label": "思考",
      "a11y": "モデルの思考モードを切り替え",
      "pending": "次のターンで反映",
      "low": "低",
      "high": "高",
      "max": "最大",
      "on": "オン",
      "off": "オフ"
    }
  },
  "ko": {
    "thinkingToggle": {
      "label": "생각",
      "a11y": "모델 생각 모드 전환",
      "pending": "다음 턴에 적용",
      "low": "낮음",
      "high": "높음",
      "max": "최대",
      "on": "켜기",
      "off": "끄기"
    }
  }
}
</i18n>
