<template>
  <!-- 独立任务页：完整占满会话主体区域，复用 SubagentTasksView 渲染该会话的后台任务列表。
       路由形态 /home/tasks/{sid}，由 home/index.vue 的 NuxtPage 承载。
       顶部无返回栏，用户经左侧侧边栏会话列表返回聊天页。 -->
  <div class="w-full h-full">
    <SubagentTasksView />

  </div>
</template>

<script lang="ts" setup>
// 页面级错误捕获：本页所有后代组件（SubagentTasksView 及其子图等）的运行时错误
// → logUtil 日志 + 全局 toast，return false 阻断向上冒泡
// （03-errorCaptured工厂函数.md 工厂函数模式）
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();

// components
import SubagentTasksView from '../../components/SubagentTasksView.vue';
</script>
