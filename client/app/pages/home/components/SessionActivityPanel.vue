<template>
  <div
    class="flex h-full min-h-0 flex-col gap-2 p-3"
    data-test="session-activity-panel">
    <!-- TaskFlow: one compact progress row per active flow. -->
    <section class="flex flex-col gap-1">
      <button
        type="button"
        class="flex w-full cursor-pointer items-center gap-1.5 text-left text-xs font-medium text-gray-600 hover:text-theme-main dark:text-gray-300"
        data-test="session-activity-toggle-taskflow"
        @click="open.taskflow = !open.taskflow">
        <i :class="['pi text-[9px]', open.taskflow ? 'pi-chevron-down' : 'pi-chevron-right']" />
        <i class="pi pi-chart-line text-[11px] text-theme-main" />
        <span>{{ t('sessionActivity.taskflow') }}</span>
        <span class="text-[10px] text-gray-400">({{ flows.length }})</span>
        <span
          class="ml-auto rounded px-1.5 py-0.5 text-[10px]"
          :class="
            taskflow.totals.total > 0
              ? 'bg-theme-main/10 text-theme-main'
              : 'bg-gray-100 text-gray-400 dark:bg-gray-800'
          ">
          {{ taskflow.totals.done }}/{{ taskflow.totals.total }}
        </span>
      </button>
      <div
        v-if="open.taskflow"
        class="flex flex-col gap-1.5 pl-5">
        <p
          v-if="!flows.length"
          class="m-0 text-[11px] text-gray-400 dark:text-gray-500"
          data-test="session-activity-taskflow-empty">
          {{ t('sessionActivity.noTaskflow') }}
        </p>
        <div
          v-for="flow in flows"
          :key="flow.flow_id"
          class="flex flex-col gap-0.5"
          :data-test="`session-activity-flow-${flow.flow_id}`">
          <div class="flex items-baseline gap-2">
            <span class="min-w-0 flex-1 truncate text-[11px] text-gray-600 dark:text-gray-300">
              {{ flow.description || flow.flow_id }}
            </span>
            <span class="shrink-0 font-mono text-[10px] text-gray-400"> {{ flow.done }}/{{ flow.total }} </span>
          </div>
          <div class="h-1 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
            <div
              class="h-full rounded-full bg-theme-main"
              :style="{ width: `${flowPercent(flow)}%` }" />
          </div>
        </div>
        <button
          type="button"
          class="cursor-pointer self-start text-[11px] text-theme-main hover:underline"
          data-test="session-activity-open-board"
          @click="openBoard">
          {{ t('sessionActivity.openBoard') }}
        </button>
      </div>
    </section>

    <!-- Sub-agents: the live runs, each opening its activity tab. -->
    <section class="flex flex-col gap-1 border-t border-solid border-gray-100 pt-2 dark:border-gray-800">
      <button
        type="button"
        class="flex w-full cursor-pointer items-center gap-1.5 text-left text-xs font-medium text-gray-600 hover:text-theme-main dark:text-gray-300"
        data-test="session-activity-toggle-subagents"
        @click="open.subagents = !open.subagents">
        <i :class="['pi text-[9px]', open.subagents ? 'pi-chevron-down' : 'pi-chevron-right']" />
        <i class="pi pi-android text-[11px] text-theme-main" />
        <span>{{ t('sessionActivity.subagents') }}</span>
        <span class="text-[10px] text-gray-400">({{ activeRuns.length }})</span>
      </button>
      <div
        v-if="open.subagents"
        class="flex flex-col gap-1 pl-5">
        <p
          v-if="!activeRuns.length"
          class="m-0 text-[11px] text-gray-400 dark:text-gray-500"
          data-test="session-activity-subagents-empty">
          {{ t('sessionActivity.noSubagents') }}
        </p>
        <button
          v-for="run in activeRuns"
          :key="run.run_id"
          type="button"
          class="row-button"
          :data-test="`session-activity-run-${run.run_id}`"
          :title="statusLabel(run)"
          @click="openActivity(run.run_id)">
          <i :class="['shrink-0 text-[11px]', statusIcon(run)]" />
          <span class="min-w-0 flex-1 truncate text-left text-[11px]">{{ runLabel(run) }}</span>
          <span class="shrink-0 text-[10px] text-gray-400">{{ roleLabel(run) }}</span>
          <span class="shrink-0 font-mono text-[10px] text-gray-400">
            {{ formatElapsed(run.execution?.started_at, now) }}
          </span>
        </button>
      </div>
    </section>

    <!-- Foreground tools: the calls executing right now. -->
    <section class="flex flex-col gap-1 border-t border-solid border-gray-100 pt-2 dark:border-gray-800">
      <button
        type="button"
        class="flex w-full cursor-pointer items-center gap-1.5 text-left text-xs font-medium text-gray-600 hover:text-theme-main dark:text-gray-300"
        data-test="session-activity-toggle-tools"
        @click="open.tools = !open.tools">
        <i :class="['pi text-[9px]', open.tools ? 'pi-chevron-down' : 'pi-chevron-right']" />
        <i class="pi pi-bolt text-[11px] text-theme-main" />
        <span>{{ t('sessionActivity.tools') }}</span>
        <span class="text-[10px] text-gray-400">({{ commands.length }})</span>
      </button>
      <div
        v-if="open.tools"
        class="flex flex-col gap-1 pl-5">
        <p
          v-if="!commands.length"
          class="m-0 text-[11px] text-gray-400 dark:text-gray-500"
          data-test="session-activity-tools-empty">
          {{ t('sessionActivity.noTools') }}
        </p>
        <div
          v-for="command in commands"
          :key="command.id"
          class="flex items-center gap-2 text-[11px] text-gray-600 dark:text-gray-300"
          :data-test="`session-activity-tool-${command.id}`">
          <i class="pi pi-spin pi-spinner shrink-0 text-[10px] text-theme-main" />
          <span class="min-w-0 flex-1 truncate font-mono">{{ command.summary }}</span>
          <span class="shrink-0 font-mono text-[10px] text-gray-400">
            {{ formatElapsed(command.startedAtMs, now) }}
          </span>
        </div>
      </div>
    </section>
  </div>
</template>

<script lang="ts" setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { FlowProgress } from '~/stores/taskflow';
import type { SubagentRun } from '~/composables/bridge';
import { formatElapsed } from '~/common/utils';
import { isActiveRun } from '~/utils/subagent';

const { t } = useI18n({ useScope: 'local' });

const taskflow = useTaskflowStore();
const subagentStore = useSubagentStore();
const commandsStore = useRunningCommandsStore();
const rightSidebar = useRightSidebarStore();
const { roleLabel, runLabel, statusLabel } = useSubagentTasks();

/** Which sections are expanded (all three by default: this IS the overview). */
const open = reactive({ taskflow: true, subagents: true, tools: true });

const flows = computed<FlowProgress[]>(() => taskflow.flows ?? []);
const activeRuns = computed(() => (subagentStore.taskRuns ?? []).filter(isActiveRun));
const commands = computed(() => commandsStore.commands ?? []);

/** Ticking clock for the elapsed columns (while anything runs). */
const now = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | undefined;
const syncTicker = (): void => {
  const wanted = activeRuns.value.length > 0 || commands.value.length > 0;
  if (wanted && !ticker) {
    ticker = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  } else if (!wanted && ticker) {
    clearInterval(ticker);
    ticker = undefined;
  }
};
watch([() => activeRuns.value.length, () => commands.value.length], syncTicker, { immediate: true });
onBeforeUnmount(() => {
  if (ticker) clearInterval(ticker);
});

/**
 * One flow's completion percentage.
 * @param flow
 */
const flowPercent = (flow: FlowProgress): number => (flow.total > 0 ? Math.round((flow.done / flow.total) * 100) : 0);

/**
 * The run's status glyph (spinner / pause / clock).
 * @param run
 */
const statusIcon = (run: SubagentRun): string => {
  const status = String(run.execution?.status ?? '').toUpperCase();
  if (status === 'RUNNING') return 'pi pi-spin pi-spinner text-theme-main';
  if (status === 'INTERRUPTED') return 'pi pi-pause-circle text-amber-500 dark:text-amber-400';
  return 'pi pi-clock text-gray-400';
};

/** Open the full TaskFlow board tab. */
const openBoard = (): void => {
  rightSidebar.openTab('taskFlowBoard');
};

/**
 * Open one run's activity tab.
 * @param runId
 */
const openActivity = (runId: string): void => {
  rightSidebar.openTab('subagentActivity', { runId });
};
</script>

<style scoped>
/* One sub-agent row: glyph / title / role / elapsed on one line. */
.row-button {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  border-radius: 0.375rem;
  padding: 0.2rem 0.3rem;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.row-button:hover {
  background-color: rgba(107, 114, 128, 0.12);
}
</style>

<i18n lang="json">
{
  "zh": {
    "sessionActivity": {
      "taskflow": "任务流",
      "subagents": "子代理",
      "tools": "前台工具",
      "noTaskflow": "暂无进行中的任务流。",
      "noSubagents": "暂无运行中的子代理。",
      "noTools": "暂无执行中的工具调用。",
      "openBoard": "打开任务看板"
    }
  },
  "en": {
    "sessionActivity": {
      "taskflow": "Task flows",
      "subagents": "Sub-agents",
      "tools": "Foreground tools",
      "noTaskflow": "No active task flows.",
      "noSubagents": "No sub-agents running.",
      "noTools": "No foreground tool calls running.",
      "openBoard": "Open the task board"
    }
  },
  "ja": {
    "sessionActivity": {
      "taskflow": "タスクフロー",
      "subagents": "サブエージェント",
      "tools": "フォアグラウンドツール",
      "noTaskflow": "進行中のタスクフローはありません。",
      "noSubagents": "実行中のサブエージェントはありません。",
      "noTools": "実行中のツール呼び出しはありません。",
      "openBoard": "タスクボードを開く"
    }
  },
  "ko": {
    "sessionActivity": {
      "taskflow": "작업 흐름",
      "subagents": "서브에이전트",
      "tools": "포그라운드 도구",
      "noTaskflow": "진행 중인 작업 흐름이 없습니다.",
      "noSubagents": "실행 중인 서브에이전트가 없습니다.",
      "noTools": "실행 중인 도구 호출이 없습니다.",
      "openBoard": "작업 보드 열기"
    }
  }
}
</i18n>
