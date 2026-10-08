<template>
  <a
    href="#main-content"
    class="skip-link"
    >{{ t('a11y.skipToMain') }}</a
  >
  <main
    id="main-content"
    class="layout">
    <slot></slot>
  </main>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
</script>

<style lang="scss" scoped>
@use 'sass:math';
@use '@/common.scss' as common;

.layout {
  @include common.fullViewWindow;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
</style>

<i18n lang="json">
{
  "zh": {
    "a11y": {
      "skipToMain": "跳到主内容"
    }
  },
  "en": {
    "a11y": {
      "skipToMain": "Skip to main content"
    }
  },
  "ja": {
    "a11y": {
      "skipToMain": "メインコンテンツへスキップ"
    }
  },
  "ko": {
    "a11y": {
      "skipToMain": "메인 콘텐츠로 건너뛰기"
    }
  }
}
</i18n>
