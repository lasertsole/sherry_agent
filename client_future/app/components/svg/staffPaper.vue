<template>
    <svg class="staffPaper" ref="staffPaper" viewBox="0 0 100 100">
        <template v-for="value in staffNumOfcurrentPage">
            <SvgStaff
                :heightPercent = "heightPercent"
                :translateY = "paddingY + ((value-1) * baisPerStaff)"
            ></SvgStaff>
        </template>
    </svg>
</template>

<script lang="ts" setup>
    const staffConfigInstance: StaffConfig = StaffConfig.getInstance();//DM服务实例
    const paddingY: Ref<number> = staffConfigInstance.getPaddingY();
    const heightPercent: Ref<number> = staffConfigInstance.getHeightPercent();
    const staffNumOfcurrentPage: ComputedRef<number> = staffConfigInstance.getStaffNumOfcurrentPage();
    const baisPerStaff = staffConfigInstance.getBaisPerStaff();
    
    const staffPaper:Ref<HTMLElement | undefined> = ref<HTMLElement | undefined>();
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .staffPaper{
        @include common.fullViewWindow;
    }
</style>