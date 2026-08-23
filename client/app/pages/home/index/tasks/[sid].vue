<template>
  <!-- 独立任务页：完整占满会话主体区域，复用 SubagentTasksView 渲染该会话的后台任务列表。
       路由形态 /home/tasks/{sid}，由 home/index.vue 的 NuxtPage 承载。
       顶部无返回栏，用户经左侧侧边栏会话列表返回聊天页。 -->
  <div class="w-full h-full">
    <SubagentTasksView />
  </div>
</template>

<script lang="ts" setup>
// components
import SubagentTasksView from '../../components/SubagentTasksView.vue';
</script>
