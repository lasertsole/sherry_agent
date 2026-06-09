<template>
    <input v-model="chatInput" :placeholder="$t('chatInput')" class="inputBox">
</template>

<script lang="ts" setup>
const chatInput:Ref<string> = ref("")
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .inputBox{
        background-color: rgba(0, 0, 0, 0.05);
        height: fit-content;
        width: clamp(150px, 75%, 800px);
        position: absolute;
        bottom: 1rem;
        left: 50%;
        transform: translateX(-50%);
        border-radius: 5px;
        padding: 5px 10px;
        outline: none;
    }
</style>