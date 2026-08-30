<template>
  <!-- Standalone tasks page: fully fills the session main area, reusing SubagentTasksView to render
       this session's background task list.
       Route form /home/tasks/{sid}, hosted by home/index.vue's NuxtPage.
       No back bar at the top; users return to the chat page via the session list in the left sidebar. -->
  <div class="w-full h-full">
    <SubagentTasksView />
  </div>
</template>

<script lang="ts" setup>
// Page-level error capture: runtime errors from all descendant components of this page (SubagentTasksView
// and its sub-graphs, etc.) → logUtil log + global toast; return false stops propagation upward
// (03-errorCaptured factory function doc, factory function pattern)
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();

// components
import SubagentTasksView from '../../components/SubagentTasksView.vue';
</script>
