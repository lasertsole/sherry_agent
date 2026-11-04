<template>
  <!-- Terminal entry: how many runs/commands this session still owns — live
       sub-agents, foreground tool calls AND the commands the agent detached
       (terminal(run_in_background=True)). Clicking opens the summary panel above
       the toolbar, which is where a detached command is watched and stopped;
       picking a row asks the session page to show where that work actually lives
       — a sub-agent opens its live context view, a command scrolls the chat to
       its tool card (its terminal output) — instead of opening a panel of its
       own. -->
  <ToolbarPopover ref="popover">
    <template #trigger="{ toggle, open }">
      <button
        type="button"
        class="relative flex cursor-pointer items-center justify-center rounded-full p-1 transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
        :title="tooltip"
        :aria-label="t('tasksButton.label')"
        @click="togglePopover($event, toggle, open)">
        <i class="terminal-icon text-sm text-gray-500 dark:text-gray-400"></i>
        <span
          v-if="activeCount"
          class="absolute -top-0.5 -right-0.5 flex min-w-[15px] h-[15px] items-center justify-center rounded-full px-1 text-[10px] leading-none font-medium text-white bg-red-500">
          {{ activeCount > 99 ? '99+' : activeCount }}
        </span>
      </button>
    </template>

    <div class="flex w-72 flex-col gap-3">
      <!-- Running sub-agents: one ZCode-directory-style row each (status icon,
           role, task summary, elapsed). Clicking opens the run's activity tab. -->
      <section class="flex flex-col gap-1.5">
        <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
          {{ t('tasksButton.subagents', { count: activeRuns.length }) }}
        </span>
        <ul
          v-if="activeRuns.length"
          class="flex flex-col gap-1">
          <li
            v-for="run in activeRuns"
            :key="run.run_id">
            <button
              type="button"
              class="row-button"
              :title="`${statusLabel(run)} · ${runLabel(run)}`"
              @click="openRun(run.run_id)">
              <i
                :class="['shrink-0 text-[11px]', statusIcon(run)]"
                :data-test="`tasks-run-icon-${run.run_id}`" />
              <span class="flex min-w-0 flex-1 flex-col">
                <span class="flex min-w-0 items-baseline gap-1.5">
                  <span class="min-w-0 flex-1 truncate text-left">{{ runLabel(run) }}</span>
                  <span class="shrink-0 text-[10px] text-gray-400">{{ roleLabel(run) }}</span>
                </span>
                <span
                  v-if="taskSummary(run)"
                  class="truncate text-left text-[10px] text-gray-400 dark:text-gray-500">
                  {{ taskSummary(run) }}
                </span>
              </span>
              <span class="shrink-0 font-mono">{{ formatElapsed(run.execution?.started_at, now) }}</span>
            </button>
          </li>
        </ul>
        <span
          v-else
          class="text-xs text-gray-400 dark:text-gray-500">
          {{ t('tasksButton.none') }}
        </span>
      </section>

      <!-- Ended sub-agents (collapsed by default): the history of this session's
           runs. Clicking a row opens the same activity tab (read-only). -->
      <section
        v-if="endedRuns.length"
        class="flex flex-col gap-1.5 border-t border-solid border-gray-100 pt-2 dark:border-gray-800">
        <button
          type="button"
          class="flex cursor-pointer items-center gap-1 self-start text-xs font-medium text-gray-500 hover:text-theme-main dark:text-gray-400"
          data-test="tasks-ended-toggle"
          @click="endedOpen = !endedOpen">
          <i :class="['pi text-[9px]', endedOpen ? 'pi-chevron-down' : 'pi-chevron-right']" />
          {{ t('tasksButton.ended', { count: endedRuns.length }) }}
        </button>
        <ul
          v-if="endedOpen"
          class="flex max-h-48 flex-col gap-1 overflow-y-auto">
          <li
            v-for="run in endedRuns"
            :key="run.run_id">
            <button
              type="button"
              class="row-button"
              :title="statusLabel(run)"
              :data-test="`tasks-ended-${run.run_id}`"
              @click="openRun(run.run_id)">
              <i :class="['shrink-0 text-[11px]', outcomeIcon(run)]" />
              <span class="min-w-0 flex-1 truncate text-left">{{ runLabel(run) }}</span>
              <span class="shrink-0 text-[10px] text-gray-400">{{ statusLabel(run) }}</span>
              <span class="shrink-0 font-mono">{{ ago(run.execution?.ended_at) }}</span>
            </button>
          </li>
        </ul>
      </section>

      <!-- Running commands (tool calls executing right now) -->
      <section class="flex flex-col gap-1.5 border-t border-solid border-gray-100 pt-2 dark:border-gray-800">
        <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
          {{ t('tasksButton.commands', { count: commands.length }) }}
        </span>
        <ul
          v-if="commands.length"
          class="flex flex-col gap-1">
          <li
            v-for="command in commands"
            :key="command.id">
            <button
              type="button"
              class="row-button"
              @click="openCommand(command.id)">
              <span class="shrink-0 pi pi-spin pi-spinner text-[10px] text-theme-main"></span>
              <span class="min-w-0 flex-1 truncate text-left font-mono">{{ command.summary }}</span>
              <span class="shrink-0 font-mono">{{ formatElapsed(command.startedAtMs, now) }}</span>
            </button>
          </li>
        </ul>
        <span
          v-else
          class="text-xs text-gray-400 dark:text-gray-500">
          {{ t('tasksButton.none') }}
        </span>
      </section>
      <!-- Background commands the agent detached (terminal(run_in_background=True)).
           They outlive the turn that started them, so their stop control lives
           here rather than in the chat. The section renders nothing when the
           session never backgrounded a command. -->
      <BackgroundCommandsSection
        :tasks="backgroundTasks"
        :killing="backgroundKilling"
        :kill-refusal="backgroundKillRefusal"
        :kill="backgroundKill" />

      <!-- The full overview: TaskFlow progress + sub-agents + foreground tools
           in one session-scoped panel (this popover is the short version). -->
      <button
        type="button"
        class="flex cursor-pointer items-center gap-1.5 self-start border-t border-solid border-gray-100 pt-2 text-xs text-theme-main hover:underline dark:border-gray-800"
        data-test="tasks-open-activity"
        @click="openActivity">
        <i class="pi pi-bolt text-[10px]" />
        {{ t('tasksButton.activity') }}
      </button>
    </div>
  </ToolbarPopover>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';
import BackgroundCommandsSection from './BackgroundCommandsSection.vue';
import type { BackgroundTaskItem, KillRefusal } from '~/composables/use-background-tasks';
import { isActiveRun } from '~/utils/subagent';
import { formatElapsed } from '~/common/utils';

const props = withDefaults(
  defineProps<{
    sessionId: string;
    /** Settled snapshot of this session's detached commands (`useBackgroundTasks`). */
    backgroundTasks?: BackgroundTaskItem[];
    /** How many of them still run (the badge counts them too). */
    backgroundRunning?: number;
    /** Task ids whose kill was acknowledged but whose exit has not arrived. */
    backgroundKilling?: Set<string>;
    /** The refusal the backend sent for the last kill (shown on that row once). */
    backgroundKillRefusal?: KillRefusal | null;
    /**
     * (Re)pull this session's detached commands — the page owns the snapshot
     * fetch. Called every time the popover OPENS: a command that settled while
     * it was closed must not read as running.
     */
    backgroundReload?: () => void;
    /** Kill one detached command (the page owns the socket). */
    backgroundKill?: (taskId: string) => void;
  }>(),
  {
    backgroundTasks: () => [],
    backgroundRunning: 0,
    backgroundKilling: () => new Set<string>(),
    backgroundKillRefusal: null,
    backgroundReload: () => {},
    backgroundKill: () => {}
  }
);

const emit = defineEmits<{
  /**
   * A row was picked: the session page owns the jump (`run` → the run's live
   * context view, `command` → that command's tool card in the chat).
   */
  focus: [payload: { kind: 'run' | 'command'; id: string | number }];
}>();

const { t } = useI18n({ useScope: 'local' });

const store = useSubagentStore();
const commandsStore = useRunningCommandsStore();
// Shared run labels: the role / status vocabulary lives in the subagent facade
// so the activity tab and this list can never disagree.
const { roleLabel, runLabel, statusLabel } = useSubagentTasks();
const rightSidebar = useRightSidebarStore();

/** This session's runs that still own a slot (running / interrupted / queued). */
const activeRuns = computed(() => (store.taskRuns ?? []).filter(isActiveRun));

/** Recently finished runs (newest first, bounded): the collapsed history list. */
const endedRuns = computed(() =>
  (store.allTaskRuns ?? [])
    .filter(run => !isActiveRun(run))
    .sort((a, b) => Number(b.execution?.ended_at ?? 0) - Number(a.execution?.ended_at ?? 0))
    .slice(0, 20)
);
/** Whether the ended list is expanded. */
const endedOpen = ref(false);

/** Tool calls executing right now (fed by the session page). */
const commands = computed(() => commandsStore.commands);

/** The badge counts every unit of work that still owns a slot: sub-agents,
 *  foreground tool calls and detached background commands. */
const activeCount = computed(() => activeRuns.value.length + commands.value.length + props.backgroundRunning);

/** Ticking clock for the elapsed columns (only while something is active). */
const now = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | undefined;

const syncTicker = () => {
  const wanted = activeCount.value > 0 || endedOpen.value;
  if (wanted && !ticker) {
    ticker = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  } else if (!wanted && ticker) {
    clearInterval(ticker);
    ticker = undefined;
  }
};

watch([activeCount, endedOpen], syncTicker, { immediate: true });

onMounted(() => {
  // The entry that shows the count owns the initial load now that the chat's
  // jump bar is gone: initTasks fetches this session's runs and registers the
  // sub-agent websocket (idempotent singleton).
  if (props.sessionId) initTasks(props.sessionId);
});

onBeforeUnmount(() => {
  if (ticker) clearInterval(ticker);
});

const popover = ref<{ close: () => void }>();

/**
 * Open or close the summary panel. Opening refreshes what the rows claim: the
 * sub-agent list, and the page's snapshot of detached background commands (a
 * command may have settled while the panel was closed, and the exit frame is
 * best-effort — the panel must not keep calling a finished command "running").
 * @param event Click event from the trigger.
 * @param toggle The popover's own toggle (bound through the trigger slot).
 * @param isOpen Whether the panel is open BEFORE this click.
 */
const togglePopover = (event: Event, toggle: () => void, isOpen: boolean) => {
  void event;
  toggle();
  if (isOpen) return;
  if (props.sessionId) void loadTaskRuns(props.sessionId);
  props.backgroundReload();
};

/** One line per active run/command for the hover tooltip. */
const tooltip = computed(() => {
  const header = t('tasksButton.label');
  const lines = [
    ...activeRuns.value.map(run => `${runLabel(run)} · ${formatElapsed(run.execution?.started_at, now.value)}`),
    ...commands.value.map(command => `${command.summary} · ${formatElapsed(command.startedAtMs, now.value)}`),
    ...props.backgroundTasks
      .filter(task => task.status === 'running')
      .map(task => `${task.command || task.taskId} · ${t('backgroundTasks.statusRunning')}`)
  ];
  return lines.length ? [header, ...lines].join('\n') : `${header} · ${t('tasksButton.idle')}`;
});

/**
 * One line of the run's task text, clipped (hidden when it repeats the title).
 * @param run
 */
const taskSummary = (run: SubagentRun): string => {
  const line = (run.task ?? '').split('\n')[0]?.trim() ?? '';
  if (!line) return '';
  const clipped = line.length > 60 ? `${line.slice(0, 60)}…` : line;
  return clipped === runLabel(run) ? '' : clipped;
};

/**
 * The status GLYPH: a spinner while running, pause for an interrupted run,
 * clock for a queued one.
 * @param run
 */
const statusIcon = (run: SubagentRun): string => {
  const status = String(run.execution?.status ?? '').toUpperCase();
  if (status === 'RUNNING') return 'pi pi-spin pi-spinner text-theme-main';
  if (status === 'INTERRUPTED') return 'pi pi-pause-circle text-amber-500 dark:text-amber-400';
  if (status === 'PENDING') return 'pi pi-clock text-gray-400';
  return 'pi pi-question-circle text-gray-400';
};

/**
 * The outcome glyph of a finished run (success / failure / neutral).
 * @param run
 */
const outcomeIcon = (run: SubagentRun): string => {
  const outcome = String(run.execution?.outcome?.status ?? '').toUpperCase();
  if (outcome === 'OK') return 'pi pi-check-circle text-emerald-500';
  if (outcome === 'ERROR' || outcome === 'KILLED') return 'pi pi-times-circle text-red-500';
  if (outcome === 'TIMEOUT') return 'pi pi-clock text-amber-500 dark:text-amber-400';
  return 'pi pi-flag text-gray-400';
};

/**
 * Relative "ago" text for a finished run's end timestamp.
 * @param endedAt Epoch ms, or null/undefined when unknown.
 */
const ago = (endedAt: number | null | undefined): string => {
  if (endedAt == null) return '';
  const seconds = Math.max(0, Math.floor((now.value - Number(endedAt)) / 1000));
  if (seconds < 60) return t('tasksButton.agoSeconds', { n: seconds });
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return t('tasksButton.agoMinutes', { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t('tasksButton.agoHours', { n: hours });
  return t('tasksButton.agoDays', { n: Math.floor(hours / 24) });
};

/**
 * Ask the session page to open a sub-agent run's live context view.
 * @param runId
 */
const openRun = (runId: string) => {
  emit('focus', { kind: 'run', id: runId });
  popover.value?.close();
};

/** Open the session's full activity overview tab and close this popover. */
const openActivity = () => {
  rightSidebar.openTab('sessionActivity');
  popover.value?.close();
};

/**
 * Ask the session page to reveal a running command's tool card (its terminal log).
 * @param messageId TOOL row's message id.
 */
const openCommand = (messageId: number) => {
  emit('focus', { kind: 'command', id: messageId });
  popover.value?.close();
};
</script>

<style scoped>
/* One popover row: status/summary/elapsed on a single line, hoverable. */
.row-button {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  border-radius: 0.375rem;
  padding: 0.25rem 0.375rem;
  font-size: 0.75rem;
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
    "tasksButton": {
      "label": "运行中的后台任务",
      "idle": "当前没有运行中的任务",
      "subagents": "正在运行的 Subagent（{count}）",
      "commands": "正在运行的命令（{count}）",
      "none": "暂无",
      "ended": "已结束（{count}）",
      "agoSeconds": "{n} 秒前",
      "agoMinutes": "{n} 分钟前",
      "agoHours": "{n} 小时前",
      "agoDays": "{n} 天前",
      "activity": "活动总览"
    }
  },
  "en": {
    "tasksButton": {
      "label": "Running background tasks",
      "idle": "Nothing running right now",
      "subagents": "Running sub-agents ({count})",
      "commands": "Running commands ({count})",
      "none": "None",
      "ended": "Ended ({count})",
      "agoSeconds": "{n}s ago",
      "agoMinutes": "{n}m ago",
      "agoHours": "{n}h ago",
      "agoDays": "{n}d ago",
      "activity": "Session activity"
    }
  },
  "ja": {
    "tasksButton": {
      "label": "実行中のバックグラウンドタスク",
      "idle": "現在実行中のタスクはありません",
      "subagents": "実行中のサブエージェント（{count}）",
      "commands": "実行中のコマンド（{count}）",
      "none": "なし",
      "ended": "終了済み（{count}）",
      "agoSeconds": "{n} 秒前",
      "agoMinutes": "{n} 分前",
      "agoHours": "{n} 時間前",
      "agoDays": "{n} 日前",
      "activity": "セッション実況"
    }
  },
  "ko": {
    "tasksButton": {
      "label": "실행 중인 백그라운드 작업",
      "idle": "현재 실행 중인 작업이 없습니다",
      "subagents": "실행 중인 서브에이전트({count})",
      "commands": "실행 중인 명령({count})",
      "none": "없음",
      "ended": "종료됨({count})",
      "agoSeconds": "{n}초 전",
      "agoMinutes": "{n}분 전",
      "agoHours": "{n}시간 전",
      "agoDays": "{n}일 전",
      "activity": "세션 활동"
    }
  }
}
</i18n>
