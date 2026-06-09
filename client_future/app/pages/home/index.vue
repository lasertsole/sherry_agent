<template>
    <div class="container">
        <!-- 左侧-历史记录区域 -->
        <div class="left_side"></div>
        <!-- 右侧-会话主体区域 -->
        <div class="right_side"></div>
    </div>
</template>

<script lang="ts" setup>

</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .container{
        @include common.fullViewWindow;
        @include common.flexCenter;
        display: flex;

        .left_side{
            width: 30%;
            height: 100%;
            background-color: #f1f2f5;
        }
        
        .right_side{
            width: 70%;
            height: 100%;
            background-color: rgba($color: #000000, $alpha: 0.9);
        }
    }
</style>
