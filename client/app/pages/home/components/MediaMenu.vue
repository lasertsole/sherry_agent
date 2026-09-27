<template>
  <div class="flex items-center">
    <!-- Collapsed media entry: the trigger names the category, the three upload
         kinds (image / audio / video) live in the option list, mirroring the
         per-session model picker next to it in the input toolbar. -->
    <Button
      variant="text"
      size="small"
      :label="t('mediaMenu.trigger')"
      icon="pi pi-angle-down"
      icon-pos="right"
      :aria-label="t('mediaMenu.a11y')"
      @click="toggleMenu" />
    <Menu
      ref="mediaMenu"
      :model="items"
      popup />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { tools } from '../config';

const { t } = useI18n({ useScope: 'local' });

const emit = defineEmits<{ select: [event: string] }>();

/** Popup list opened by the collapsed trigger. */
const mediaMenu = ref();

/** One option per media kind, defined once in the toolbar tool registry. */
const items = computed(() =>
  tools.map(tool => ({
    label: t(tool.toolName),
    icon: tool.icon,
    command: () => emit('select', tool.event)
  }))
);

/**
 * Open the options popup.
 * @param event
 */
const toggleMenu = (event: Event) => {
  mediaMenu.value?.toggle(event);
};
</script>

<i18n lang="json">
{
  "zh": {
    "mediaMenu": {
      "trigger": "多媒体",
      "a11y": "选择要上传的多媒体类型"
    }
  },
  "en": {
    "mediaMenu": {
      "trigger": "Media",
      "a11y": "Choose the media type to upload"
    }
  },
  "ja": {
    "mediaMenu": {
      "trigger": "メディア",
      "a11y": "アップロードするメディアの種類を選択"
    }
  },
  "ko": {
    "mediaMenu": {
      "trigger": "미디어",
      "a11y": "업로드할 미디어 종류 선택"
    }
  }
}
</i18n>
