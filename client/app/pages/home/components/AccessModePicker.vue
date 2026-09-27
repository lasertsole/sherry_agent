<template>
  <!-- Access mode: how much this session may do without asking. The trigger shows
       the CURRENT mode's shield (a question mark for the strict confirm-before-
       changes mode, a check for the default auto-edit, an exclamation for full
       access); the panel picks between them. -->
  <ToolbarPopover>
    <template #trigger="{ toggle }">
      <button
        type="button"
        class="flex cursor-pointer items-center justify-center rounded-full p-1 transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
        :title="`${t('accessMode.label')} · ${t(`accessMode.${mode}`)}`"
        :aria-label="t('accessMode.label')"
        @click="toggle">
        <i
          class="text-sm"
          :class="GLYPHS[mode]"></i>
      </button>
    </template>

    <ul class="flex w-56 flex-col gap-1">
      <li
        v-for="option in OPTIONS"
        :key="option">
        <button
          type="button"
          class="row-button"
          :class="option === mode ? 'bg-theme-main/10 text-theme-main' : ''"
          @click="handleSelect(option)">
          <i
            class="text-sm"
            :class="GLYPHS[option]"></i>
          <span class="min-w-0 flex-1 text-left">{{ t(`accessMode.${option}`) }}</span>
          <i
            v-if="option === mode"
            class="pi pi-check text-xs"></i>
        </button>
      </li>
    </ul>
  </ToolbarPopover>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';
import type { AccessMode } from '~/composables/bridge/session';

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n({ useScope: 'local' });

const store = useAccessModeStore();

/** The three positions of the control, in presentation order (strictest first). */
const OPTIONS: AccessMode[] = ['confirm_all', 'auto_edit', 'full_access'];

/** One shield glyph per mode, carrying its own colour. */
const GLYPHS: Record<AccessMode, string> = {
  confirm_all: 'shield-question-icon text-gray-500 dark:text-gray-400',
  auto_edit: 'shield-check-icon text-gray-500 dark:text-gray-400',
  full_access: 'shield-alert-icon text-amber-500'
};

/** Current mode of the session (auto-edit until the backend says otherwise). */
const mode = computed(() => store.modeFor(props.sessionId));

/** Hydrate on first mount and whenever the session changes. */
watch(
  () => props.sessionId,
  sessionId => {
    if (sessionId) store.hydrate(sessionId);
  },
  { immediate: true }
);

/**
 * Switch the session's access mode. Applies from the next tool call on (the
 * backend flag is what the approval pipeline reads, so the running turn keeps
 * the mode it started with).
 * @param option
 */
const handleSelect = async (option: AccessMode) => {
  await store.select(props.sessionId, option);
};
</script>

<style scoped>
/* One panel row: shield + name + the current-mode tick. */
.row-button {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  border-radius: 0.375rem;
  padding: 0.375rem 0.5rem;
  font-size: 0.75rem;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.row-button:hover {
  background-color: rgba(107, 114, 128, 0.12);
}
</style>

<i18n lang="json">
{
  "zh": {
    "accessMode": {
      "label": "访问模式",
      "confirm_all": "变更前确认",
      "auto_edit": "自动编辑",
      "full_access": "完全访问"
    }
  },
  "en": {
    "accessMode": {
      "label": "Access mode",
      "confirm_all": "Confirm changes",
      "auto_edit": "Auto edit",
      "full_access": "Full access"
    }
  },
  "ja": {
    "accessMode": {
      "label": "アクセスモード",
      "confirm_all": "変更前に確認",
      "auto_edit": "自動編集",
      "full_access": "フルアクセス"
    }
  },
  "ko": {
    "accessMode": {
      "label": "접근 모드",
      "confirm_all": "변경 전 확인",
      "auto_edit": "자동 편집",
      "full_access": "전체 접근"
    }
  }
}
</i18n>
