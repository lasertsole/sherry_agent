<template>
    <main class="layout">
        <slot></slot>
    </main>
</template>

<script setup lang="ts">
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .layout{
        @include common.fullViewWindow;
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }
</style>