<template>
    <div class="root">
        <ChatInputBox></ChatInputBox>
    </div>
</template>

<script lang="ts" setup>

</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .root{
        @include common.fullViewWindow;
        position: relative;
    }
</style>