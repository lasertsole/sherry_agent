<template>
  <div class="root">
    <ChatInputBox></ChatInputBox>
  </div>
</template>

<script lang="ts" setup>
// Page-level error capture: runtime errors from all descendant components of this page → logUtil log + global toast;
// return false stops propagation upward (03-errorCaptured factory function doc, factory function pattern)
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();
</script>

<style lang="scss" scoped>
@use 'sass:math';
@use '@/common.scss' as common;

.root {
  @include common.fullViewWindow;
  position: relative;
}
</style>
