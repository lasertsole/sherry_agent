<template>
  <!-- Icon-only (like its top-bar neighbours). Clicking opens the session's
       preset VIEW in the right sidebar's 当前会话 group — there is no popup here:
       the panel is a tab, so it survives outside clicks and resizes like any
       other tool. The title names the preset, and at a squeezed column the
       button hides with the folder button. -->
  <Button
    variant="text"
    icon="pi pi-user"
    :title="triggerTitle"
    :aria-label="triggerTitle"
    class="text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 @max-[300px]:hidden!"
    data-test="session-preset-trigger"
    @click="openPresetTab" />
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionPresetBinding } from '@/composables/db';
import { logUtil } from '~/utils/log';

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();

/** The right sidebar hosts the preset view as a tab (当前会话 group). */
const rightSidebar = useRightSidebarStore();

/** This session's preset binding (`null` = created before the binding existed). */
const binding = ref<SessionPresetBinding | null>(null);

/** Tooltip / accessible name of the trigger. */
const triggerTitle = computed(() =>
  binding.value ? t('personaPreset.currentWithName', { name: binding.value.preset_name }) : t('personaPreset.unbound')
);

/** Open (or activate) the session preset tab in the right sidebar. */
const openPresetTab = () => {
  rightSidebar.openTab('sessionPreset');
};

/** Read this session's binding — used only for the tooltip. */
const loadBinding = async () => {
  try {
    binding.value = (await readCachedSessionPreset(props.sessionId)) ?? null;
  } catch (e) {
    logUtil.e('[SessionPresetButton] Failed to read the preset binding:', e);
    binding.value = null;
  }
};

watch(() => props.sessionId, loadBinding, { immediate: true });
</script>

<i18n lang="json">
{
  "en": {
    "personaPreset": {
      "currentWithName": "Session preset & role: {name}"
    }
  },
  "zh": {
    "personaPreset": {
      "currentWithName": "当前会话预设角色：{name}"
    }
  },
  "ja": {
    "personaPreset": {
      "currentWithName": "現在のセッションのプリセットと役割：{name}"
    }
  },
  "ko": {
    "personaPreset": {
      "currentWithName": "현재 세션 프리셋과 역할: {name}"
    }
  }
}
</i18n>
