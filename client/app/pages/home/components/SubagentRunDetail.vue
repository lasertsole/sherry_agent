<template>
  <div class="flex flex-col h-full min-h-0 overflow-y-auto p-4">
    <!-- 空态：未选中任何节点 -->
    <div
      v-if="!run"
      class="flex flex-col items-center justify-center gap-3 h-full text-center py-8">
      <i class="pi pi-hand-pointer text-3xl text-gray-300 dark:text-gray-600" />
      <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('taskDetail.placeholder') }}</span>
    </div>

    <!-- 详情内容 -->
    <div v-else class="flex flex-col gap-3">
      <!-- 头部：任务名 + 状态徽章 + 角色/深度 -->
      <div class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
        <div class="flex items-start gap-3">
          <div class="flex-1 min-w-0">
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100 break-words">
              {{ runLabel(run) }}
            </div>
            <div class="flex flex-wrap items-center gap-2 mt-1 text-xs text-gray-500 dark:text-gray-400">
              <span>{{ roleLabel(run) }}</span>
              <template v-if="run.depth != null">
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span>{{ t('taskDetail.depth') }} {{ run.depth }}</span>
              </template>
              <template v-if="run.agent_id">
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="truncate">{{ t('taskDetail.agentId') }}: {{ run.agent_id }}</span>
              </template>
            </div>
          </div>
          <span :class="['shrink-0 px-2 py-0.5 rounded-full text-xs font-medium', badgeClass(run)]">
            {{ statusLabel(run) }}
          </span>
        </div>
      </div>

      <!-- 任务描述 -->
      <div
        v-if="run.task"
        class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
        <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
          {{ t('taskDetail.taskDesc') }}
        </div>
        <div class="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words">
          {{ run.task }}
        </div>
      </div>

      <!-- 执行信息 -->
      <div class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
          <div class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.status') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ statusLabel(run) }}</span>
          </div>
          <div class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.delivery') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ deliveryLabel }}</span>
          </div>
          <div v-if="run.spawn_mode" class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.spawnMode') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ run.spawn_mode }}</span>
          </div>
          <div class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.parentSession') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right truncate" :title="run.requester_session_key">
              {{ parentSessionLabel(run) }}
            </span>
          </div>
          <div class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.startedAt') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ formatTime(run.execution.started_at) }}</span>
          </div>
          <div class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.endedAt') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ formatTime(run.execution.ended_at) }}</span>
          </div>
          <div v-if="run.ended_reason" class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.endedReason') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ run.ended_reason }}</span>
          </div>
        </div>
      </div>

      <!-- 执行结果 / 错误 -->
      <div
        v-if="run.execution.outcome?.status"
        class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
        <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
          {{ t('taskDetail.outcome') }}
        </div>
        <div class="flex flex-col gap-1.5 text-sm">
          <div class="flex justify-between gap-4">
            <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ t('taskDetail.outcomeStatus') }}</span>
            <span class="text-gray-800 dark:text-gray-200 text-right">{{ run.execution.outcome.status }}</span>
          </div>
          <div
            v-if="run.execution.outcome.error"
            class="mt-1 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-xs text-red-700 dark:text-red-300 whitespace-pre-wrap break-words">
            <span class="font-medium">{{ t('taskDetail.error') }}: </span>{{ run.execution.outcome.error }}
          </div>
        </div>
      </div>

      <!-- 返回内容 -->
      <div
        v-if="run.completion.result_text"
        class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
        <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
          {{ t('taskDetail.resultText') }}
        </div>
        <div class="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words">
          {{ run.completion.result_text }}
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import dayjs from 'dayjs';
import type { SubagentRun } from '@/composables/bridge';
import { useSubagentTasks } from '@/composables/useSubagentTasks';

const props = defineProps<{
  /** 当前选中的运行记录；undefined 表示未选中任何节点 */
  run?: SubagentRun;
}>();

const { t } = useI18n();
const { badgeClass, statusLabel, roleLabel, runLabel, parentSessionLabel } = useSubagentTasks();

/** delivery.status 文案（复用 sidebar 状态 key，缺省显示原值） */
const deliveryLabel = computed(() => {
  const key = props.run?.delivery?.status;
  if (!key) return '-';
  const mapped: Record<string, string> = {
    PENDING: t('sidebar.statusPending'),
    IN_PROGRESS: t('sidebar.statusInProgress'),
    DELIVERED: t('sidebar.statusDelivered')
  };
  return mapped[key] ?? key;
});

/** 渲染执行时间：epoch 毫秒 → 本地可读字符串；空值显示占位符 '-' */
function formatTime(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(Number(ms))) return '-';
  return dayjs(Number(ms)).format('YYYY-MM-DD HH:mm:ss');
}
</script>
