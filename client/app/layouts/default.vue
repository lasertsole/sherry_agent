<template>
  <a
    href="#main-content"
    class="skip-link"
    >{{ t('a11y.skipToMain') }}</a
  >
  <main
    id="main-content"
    class="layout">
    <!-- The desktop shell draws its own title bar (the window has no OS
         decorations); it is fixed, so it takes no place in this flex column.
         The Web build renders nothing here. -->
    <TitleBar />
    <slot></slot>
  </main>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import TitleBar from '~/components/TitleBar.vue';

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
