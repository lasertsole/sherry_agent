<template>
  <!-- Right-sidebar tab body: the background-task view (run tree + the selected
       run's detail) for the session on screen. Mounted and unmounted with its
       tab, so the mount is what initializes the run list. -->
  <div class="flex h-full min-h-0 flex-col">
    <SubagentTasksView in-panel />
  </div>
</template>

<script lang="ts" setup>
import { onMounted, onUnmounted } from 'vue';
import SubagentTasksView from './SubagentTasksView.vue';

const { setTasksTabActive } = useSubagentTasks();

// While this tab is open the background-task view is the visible one, which is
// what lets the sub-agent websocket gap-fill on (re)connect — the same flag the
// embedded view used to hold.
onMounted(() => setTasksTabActive(true));
onUnmounted(() => setTasksTabActive(false));
</script>
