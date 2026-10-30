<template>
  <!-- Background commands of this session (the agent's
       terminal(run_in_background=True)), as the third section of the toolbar's
       background-task popup. Renders nothing when there is nothing to show, so a
       session that never backgrounded a command does not grow an empty block. -->
  <section
    v-if="tasks.length > 0"
    class="flex flex-col gap-1.5 border-t border-solid border-gray-100 pt-2 dark:border-gray-800"
    data-test="background-commands">
    <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
      {{ t('backgroundTasks.title') }}
      <span class="text-gray-400 dark:text-gray-500">
        {{ t('backgroundTasks.runningCount', { running: runningCount, total: tasks.length }) }}
      </span>
    </span>
    <ul class="m-0 flex list-none flex-col gap-1 p-0">
      <li
        v-for="task in tasks"
        :key="task.taskId"
        class="flex items-center gap-2 rounded px-1 py-1 text-xs"
        :data-task-id="task.taskId">
        <!-- A requested stop shows its own chip: the row must never read
             "running" while its stop control is already unusable (the exit
             frame is a best-effort push; the snapshot settles the final state). -->
        <span
          class="shrink-0 rounded px-1.5 py-0.5 text-[0.65rem]"
          :class="
            task.status === 'running' && killing.has(task.taskId)
              ? 'bg-amber-500/20 text-amber-600 dark:text-amber-400'
              : statusClass(task)
          "
          data-test="background-task-status">
          {{
            task.status === 'running' && killing.has(task.taskId)
              ? t('backgroundTasks.statusStopping')
              : statusLabel(task)
          }}
        </span>
        <span
          class="min-w-0 flex-1 truncate font-mono text-gray-700 dark:text-gray-300"
          :title="rowTitle(task)">
          {{ task.command || task.taskId }}
        </span>
        <span
          v-if="task.taskId === killRefusal?.taskId"
          class="shrink-0 text-red-500"
          data-test="background-task-kill-refused">
          {{ t('backgroundTasks.killRefused') }}
        </span>
        <button
          v-if="task.status === 'running'"
          type="button"
          class="shrink-0 cursor-pointer rounded px-1.5 py-0.5 transition-colors disabled:opacity-50"
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
  </section>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';

import type { KillRefusal, BackgroundTaskItem } from '~/composables/use-background-tasks';

/**
 * A VIEW over `useBackgroundTasks`: the session page owns the composable (it
 * installs the socket handlers and loads the snapshot), this section only
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

/**
 * What the row's hover tooltip carries: the command plus the pid/exit code the
 * compact popup row has no room for.
 * @param task
 */
const rowTitle = (task: BackgroundTaskItem): string => {
  const parts = [task.command || task.taskId];
  if (task.pid) parts.push(`pid ${task.pid}`);
  if (task.exitCode !== null) parts.push(`exit ${task.exitCode}`);
  return parts.join(' · ');
};
</script>
