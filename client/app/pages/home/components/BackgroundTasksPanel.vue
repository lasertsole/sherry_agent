<template>
  <!-- Background commands of this session, directly under the message list.
       Renders nothing when there is nothing to show, so an ordinary session
       never grows an empty strip. -->
  <div
    v-if="tasks.length > 0"
    class="shrink-0 border-t border-solid border-gray-light dark:border-gray-dark bg-gray-50/60 dark:bg-gray-800/40"
    data-test="background-tasks">
    <button
      type="button"
      class="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs text-gray-600 dark:text-gray-300"
      :aria-expanded="expanded"
      @click="expanded = !expanded">
      <i
        class="pi text-[0.65rem]"
        :class="expanded ? 'pi-chevron-down' : 'pi-chevron-right'"
        aria-hidden="true"></i>
      <i
        class="pi pi-server text-[0.7rem]"
        aria-hidden="true"></i>
      <span class="font-medium">{{ t('backgroundTasks.title') }}</span>
      <span class="text-gray-400 dark:text-gray-500">
        {{ t('backgroundTasks.runningCount', { running: runningCount, total: tasks.length }) }}
      </span>
    </button>

    <ul
      v-if="expanded"
      class="m-0 max-h-40 overflow-y-auto px-2 pb-2 list-none"
      data-test="background-task-rows">
      <li
        v-for="task in tasks"
        :key="task.taskId"
        class="flex items-center gap-2 rounded px-1 py-1 text-xs"
        :data-task-id="task.taskId">
        <span
          class="shrink-0 rounded px-1.5 py-0.5 text-[0.65rem]"
          :class="statusClass(task)"
          data-test="background-task-status">
          {{ statusLabel(task) }}
        </span>
        <span
          class="min-w-0 flex-1 truncate font-mono text-gray-700 dark:text-gray-300"
          :title="task.command">
          {{ task.command || task.taskId }}
        </span>
        <span
          v-if="task.pid"
          class="shrink-0 text-gray-400 dark:text-gray-500"
          >pid {{ task.pid }}</span
        >
        <span
          v-if="task.taskId === killRefusal?.taskId"
          class="shrink-0 text-red-500"
          data-test="background-task-kill-refused">
          {{ t('backgroundTasks.killRefused') }}
        </span>
        <button
          v-if="task.status === 'running'"
          type="button"
          class="shrink-0 rounded px-1.5 py-0.5 transition-colors disabled:opacity-50"
          :class="'bg-red-500/30 hover:bg-red-500/50'"
          :disabled="killing.has(task.taskId)"
          :title="t('backgroundTasks.kill')"
          :aria-label="t('backgroundTasks.kill')"
          data-test="background-task-kill"
          @click="kill(task.taskId)">
          <i
            class="pi pi-times text-[0.6rem]"
            aria-hidden="true"></i>
        </button>
      </li>
    </ul>
  </div>
</template>

<script lang="ts" setup>
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';

import type { KillRefusal, BackgroundTaskItem } from '~/composables/use-background-tasks';

/**
 * The strip is a VIEW over `useBackgroundTasks`: the page owns the composable
 * (it installs the socket handlers and loads the snapshot), the strip only
 * renders what it is handed and forwards the kill click.
 */
defineProps<{
  tasks: BackgroundTaskItem[];
  runningCount: number;
  killing: Set<string>;
  killRefusal: KillRefusal | null;
  kill: (taskId: string) => void;
}>();

const { t } = useI18n();

/** Expanded by default: the strip only exists while a command is worth watching. */
const expanded = ref(true);

/**
 * Localized status label (running / done / failed / stopped).
 * @param task
 */
const statusLabel = (task: BackgroundTaskItem): string => {
  switch (task.status) {
    case 'running':
      return t('backgroundTasks.statusRunning');
    case 'completed':
      return t('backgroundTasks.statusCompleted');
    case 'failed':
      return t('backgroundTasks.statusFailed');
    case 'killed':
      return t('backgroundTasks.statusKilled');
    default:
      return task.status;
  }
};

/**
 * Colour by outcome: running neutral, done grey, failed red, stopped amber.
 * @param task
 */
const statusClass = (task: BackgroundTaskItem): string => {
  switch (task.status) {
    case 'running':
      return 'bg-theme-main/20 text-theme-main';
    case 'completed':
      return 'bg-gray-400/20 text-gray-600 dark:text-gray-300';
    case 'failed':
      return 'bg-red-500/20 text-red-600 dark:text-red-400';
    case 'killed':
      return 'bg-amber-500/20 text-amber-600 dark:text-amber-400';
    default:
      return 'bg-gray-400/20 text-gray-600 dark:text-gray-300';
  }
};
</script>
