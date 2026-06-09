<template>
  <div class="homepage_container">
    <!-- 左侧-历史记录区域 -->
    <div class="left_side"></div>
    <!-- 右侧-会话主体区域 -->
    <div class="right_side">
      <div class="header"></div>
      <div class="main"></div>
      <div class="footer"></div>
    </div>
  </div>
</template>

<script lang="ts" setup></script>

<style lang="scss" scoped>
@use 'sass:math';
@use '@/common.scss' as common;

.homepage_container {
  @include common.fullViewWindow;
  @include common.flexCenter;
  display: flex;

  .left_side {
    width: 360px;
    height: 100%;
    background-color: #2a2a36;
  }

  .right_side {
    flex: 1;
    height: 100%;
    background-color: #131619;
    display: flex;
    flex-direction: column;

    .header {
      height: 60px;
    }

    .main {
      flex: 1;
    }

    .footer {
      height: 160px;
    }
  }
}
</style>
