<template>
    <div class="root">
        <ChatInputBox></ChatInputBox>

    </div>
</template>

<script lang="ts" setup>
// 页面级错误捕获：本页所有后代组件的运行时错误 → logUtil 日志 + 全局 toast，
// return false 阻断向上冒泡（03-errorCaptured工厂函数.md 工厂函数模式）
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .root{
        @include common.fullViewWindow;
        position: relative;
    }
</style>