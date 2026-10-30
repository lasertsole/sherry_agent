<template>
  <!-- Background commands of this session (the agent's
       terminal(run_in_background=True)) that are STILL RUNNING, as the third
       section of the toolbar's background-task popup. A finished command has
       nothing left to watch or stop — its notification is the chat's background
       card, its output tail stays readable for the agent — so it is not listed
       here, and the section renders nothing when every command has settled. -->
  <section
    v-if="running.length > 0"
    class="flex flex-col gap-1.5 border-t border-solid border-gray-100 pt-2 dark:border-gray-800"
    data-test="background-commands">
    <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
      {{ t('backgroundTasks.title') }}
      <span class="text-gray-400 dark:text-gray-500">{{
        t('backgroundTasks.runningOnly', { count: running.length })
      }}</span>
    </span>
    <ul class="m-0 flex list-none flex-col gap-1 p-0">
      <li
        v-for="task in running"
        :key="task.taskId"
        class="flex items-center gap-2 rounded px-1 py-1 text-xs"
        :data-task-id="task.taskId">
        <!-- A requested stop shows its own chip: the row must never read
             "running" while its stop control is already unusable (the exit
             frame is a best-effort push; the snapshot settles the final state,
             and the row then leaves the list entirely). -->
        <span
          class="shrink-0 rounded px-1.5 py-0.5 text-[0.65rem]"
          :class="
            killing.has(task.taskId)
              ? 'bg-amber-500/20 text-amber-600 dark:text-amber-400'
              : 'bg-theme-main/20 text-theme-main'
          "
          data-test="background-task-status">
          {{ killing.has(task.taskId) ? t('backgroundTasks.statusStopping') : t('backgroundTasks.statusRunning') }}
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
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

import type { KillRefusal, BackgroundTaskItem } from '~/composables/use-background-tasks';

/**
 * A VIEW over `useBackgroundTasks`: the session page owns the composable (it
 * installs the socket handlers and loads the snapshot), this section only
 * renders what it is handed and forwards the stop click. Settled rows are
 * filtered out here — a finished command has nothing left to watch or stop
 * (its notification is the chat's background card).
 */
const props = defineProps<{
  tasks: BackgroundTaskItem[];
  killing: Set<string>;
  killRefusal: KillRefusal | null;
  kill: (taskId: string) => void;
}>();

const { t } = useI18n();

/** The rows worth a line: only a running command has a stop control. */
const running = computed(() => props.tasks.filter(task => task.status === 'running'));

/**
 * What the row's hover tooltip carries: the command plus the pid the compact
 * popup row has no room for.
 * @param task
 */
const rowTitle = (task: BackgroundTaskItem): string => {
  const parts = [task.command || task.taskId];
  if (task.pid) parts.push(`pid ${task.pid}`);
  return parts.join(' · ');
};
</script>
