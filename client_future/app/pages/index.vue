<template>
    <div class="root">
        <LazySvgStaffPaper/>
        <DomDrawer/>
    </div>
</template>

<script lang="ts" setup>
import { DomDrawer } from '#components';


</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .root{
        @include common.fullViewWindow;
    }
</style>