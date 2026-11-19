<template>
  <div
    class="task-board-nav-panel flex flex-col h-full"
    data-test="task-board-panel">
    <div class="flex items-center justify-between px-3 py-2">
      <span class="font-medium">{{ t('nav.tasks') }}</span>
      <Select
        v-model="store.globalFilter"
        :options="statusOptions"
        option-label="label"
        option-value="value"
        class="w-32 text-xs"
        data-test="task-board-filter" />
    </div>
    <div class="flex-1 overflow-y-auto px-3">
      <div
        v-if="store.globalLoading"
        class="text-center py-4"
        data-test="task-board-loading">
        …
      </div>
      <div
        v-else-if="store.filteredGlobalFlows.length === 0"
        class="text-center py-4 text-gray-400"
        data-test="task-board-empty">
        {{ t('taskBoard.empty') }}
      </div>
      <div
        v-else
        class="space-y-2">
        <ListItemCard
          v-for="flow in store.filteredGlobalFlows"
          :key="flow.flow_id"
          :icon="'pi pi-list'"
          :title="flow.description || flow.flow_id"
          :badges="[{ value: flow.status, severity: statusSeverity(flow.status) }]"
          :meta="[
            { value: `${flow.done}/${flow.total} ${t('taskBoard.stepsSuffix')}` },
            { value: flow.session_id ?? '' }
          ]"
          data-test="task-board-card"
          @click="onFlowClick(flow)" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { useTaskflowStore, type FlowProgress } from '@/stores/taskflow';
import { useRightSidebarStore } from '@/stores/right-sidebar';
import ListItemCard from '@/components/ListItemCard.vue';

const { t } = useI18n();
const store = useTaskflowStore();
const rightSidebar = useRightSidebarStore();

const statusOptions = computed(() => [
  { label: t('taskBoard.all'), value: 'all' },
  { label: t('taskBoard.running'), value: 'running' },
  { label: t('taskBoard.waiting'), value: 'waiting' },
  { label: t('taskBoard.done'), value: 'done' },
  { label: t('taskBoard.failed'), value: 'failed' }
]);

function statusSeverity(status: string): 'info' | 'success' | 'warn' | 'danger' | 'secondary' {
  switch (status) {
    case 'running':
      return 'info';
    case 'done':
      return 'success';
    case 'failed':
      return 'danger';
    default:
      return 'secondary';
  }
}

const router = useRouter();
function onFlowClick(flow: FlowProgress): void {
  // Jump to the flow's session and open its board in the right column.
  if (flow.session_id) void router.push(`/home/${flow.session_id}`);
  rightSidebar.openTab('taskFlowBoard', { sessionId: flow.session_id });
}

let timer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void store.loadAllFlows();
  timer = setInterval(() => void store.loadAllFlows(), 5000);
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
});
</script>
