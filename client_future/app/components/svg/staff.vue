<template>
    <g class="staff" ref="staff" :transform="`translate(${translateX}, ${translateY})`">
        <template v-for="value in 5">
            <line x1="0" x2="100" :y1="lineGap*value" :y2="lineGap*value" stroke="black" :stroke-width="lineBold"></line>
        </template>
    </g>
</template>

<script lang="ts" setup>
    const props = defineProps({
        translateX: {type:Number, required: false, default: 0},
        translateY: {type:Number, required: false, default: 0},
        heightPercent: {type:Number, required: false, default: 10},
        widthPercent: {type:Number, required: false, default: 100},
    });

    const lineGap = props.heightPercent / 5;
    const lineBold = props.heightPercent / 50;
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .staff{
        display: flex;
    }
</style>