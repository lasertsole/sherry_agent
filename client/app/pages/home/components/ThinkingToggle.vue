<template>
  <div class="ml-auto flex items-center gap-2 pl-2">
    <!-- Glyph + trigger are one visual unit (a word-sized gap would read as two
         separate controls), so they sit in their own tighter row. -->
    <div class="flex items-center gap-0.5">
      <!-- The control's name is a brain glyph rather than a word: the trigger next
           to it already says ON/OFF (or 低/高/最高), so a second word only adds noise. -->
      <i
        class="hidden md:inline brain-icon text-sm text-gray-500 dark:text-gray-400"
        aria-hidden="true"></i>

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
    </div>
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
      :title="t('thinkingToggle.a11y')"
      @click="cycleMobile">
      <i :class="['brain-icon text-sm', active ? 'text-theme-main' : 'text-gray-400']"></i>
    </button>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import type { ThinkingLevel } from '~/composables/bridge/session';

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
/** Per-session thinking control store (hydrated from the backend). */
const store = useThinkingStore();

/**
 * The picker's options: the session's model-declared levels (clamp order) —
 * the unified seven-level selector. The on/off switch stays for models whose
 * backend mode is `on_off` AND whose level list is the bare `off`.
 */
const levels = computed(() => store.availableLevels(props.sessionId));

/** Whether the unified level picker replaces the switch for this session. */
const unified = computed(() => levels.value.length > 1);

/** Popup list of the current mode's options, opened by the collapsed trigger. */
const thinkMenu = ref();

/** Current value of the session's thinking control (bool for on_off, level for levels). */
const currentValue = computed(() => store.current(props.sessionId));
const currentLevel = computed<ThinkingLevel>(() => {
  const value = currentValue.value;
  return typeof value === 'string' ? (value as ThinkingLevel) : value ? 'high' : 'off';
});
const thinkingOn = computed(() => currentValue.value === true);

/**
 * Trigger text: the selected option only (the list stays hidden until clicked),
 * mirroring the level picker for switch-style models.
 */
const triggerLabel = computed(() =>
  unified.value || store.mode === 'levels'
    ? t(`thinkingToggle.${currentLevel.value}`)
    : t(`thinkingToggle.${thinkingOn.value ? 'on' : 'off'}`)
);

/** Menu items for the active mode; the current option carries a check marker. */
const menuItems = computed(() => {
  if (unified.value || store.mode === 'levels') {
    return levels.value.map(lvl => ({
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
  if (unified.value) return currentLevel.value !== 'off';
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
const handleLevel = (lvl: ThinkingLevel) => {
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
  if (unified.value || store.mode === 'levels') {
    const options = levels.value;
    const index = options.indexOf(currentLevel.value);
    const next = options[(index + 1) % options.length] ?? options[0];
    if (next) store.setValue(props.sessionId, next);
    return;
  }
  store.setValue(props.sessionId, !thinkingOn.value);
};
</script>

<i18n lang="json">
{
  "zh": {
    "thinkingToggle": {
      "a11y": "控制模型思考能力",
      "pending": "下一轮生效",
      "minimal": "最低",
      "low": "低",
      "medium": "中",
      "high": "高",
      "xhigh": "超高",
      "max": "最高",
      "on": "开启",
      "off": "关闭"
    }
  },
  "en": {
    "thinkingToggle": {
      "a11y": "Toggle model thinking",
      "pending": "Applies next turn",
      "minimal": "Minimal",
      "low": "Low",
      "medium": "Medium",
      "high": "High",
      "xhigh": "Extra High",
      "max": "Max",
      "on": "On",
      "off": "Off"
    }
  },
  "ja": {
    "thinkingToggle": {
      "a11y": "モデルの思考モードを切り替え",
      "pending": "次のターンで反映",
      "minimal": "最小",
      "low": "低",
      "medium": "中",
      "high": "高",
      "xhigh": "超高",
      "max": "最大",
      "on": "オン",
      "off": "オフ"
    }
  },
  "ko": {
    "thinkingToggle": {
      "a11y": "모델 생각 모드 전환",
      "pending": "다음 턴에 적용",
      "minimal": "최소",
      "low": "낮음",
      "medium": "중간",
      "high": "높음",
      "xhigh": "특상",
      "max": "최대",
      "on": "켜기",
      "off": "끄기"
    }
  }
}
</i18n>
