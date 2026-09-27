<template>
  <!-- Terminal entry: how many runs/commands this session still owns. Clicking
       opens the summary panel above the toolbar; picking an item asks the session
       page to show where that work actually lives — a sub-agent opens its live
       context view, a command scrolls the chat to its tool card (its terminal
       output) — instead of opening a panel of its own. -->
  <ToolbarPopover ref="popover">
    <template #trigger="{ toggle }">
      <button
        type="button"
        class="relative flex cursor-pointer items-center justify-center rounded-full p-1 transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
        :title="tooltip"
        :aria-label="t('tasksButton.label')"
        @click="togglePopover($event, toggle)">
        <i class="terminal-icon text-sm text-gray-500 dark:text-gray-400"></i>
        <span
          v-if="activeCount"
          class="absolute -top-0.5 -right-0.5 flex min-w-[15px] h-[15px] items-center justify-center rounded-full px-1 text-[10px] leading-none font-medium text-white bg-red-500">
          {{ activeCount > 99 ? '99+' : activeCount }}
        </span>
      </button>
    </template>

    <div class="flex w-72 flex-col gap-3">
      <!-- Running sub-agents -->
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
              @click="openRun(run.run_id)">
              <span
                class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium"
                :class="statusClass(run)">
                {{ statusLabel(run) }}
              </span>
              <span class="min-w-0 flex-1 truncate text-left">{{ runName(run) }}</span>
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
    </div>
  </ToolbarPopover>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';
import { isActiveRun } from '~/utils/subagent';
import { formatElapsed } from '~/common/utils';

const props = defineProps<{ sessionId: string }>();

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

/** This session's runs that still own a slot (running / interrupted / queued). */
const activeRuns = computed(() => (store.taskRuns ?? []).filter(isActiveRun));

/** Tool calls executing right now (fed by the session page). */
const commands = computed(() => commandsStore.commands);

const activeCount = computed(() => activeRuns.value.length + commands.value.length);

/** Ticking clock for the elapsed columns (only while something is active). */
const now = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | undefined;

const syncTicker = () => {
  const wanted = activeCount.value > 0;
  if (wanted && !ticker) {
    ticker = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  } else if (!wanted && ticker) {
    clearInterval(ticker);
    ticker = undefined;
  }
};

watch(activeCount, syncTicker, { immediate: true });

onMounted(() => {
  if (props.sessionId) void loadTaskRuns(props.sessionId);
});

onBeforeUnmount(() => {
  if (ticker) clearInterval(ticker);
});

const popover = ref<{ close: () => void }>();

/**
 * Open the summary panel (and refresh the run list behind it).
 * @param event Click event from the trigger.
 * @param toggle The popover's own toggle (bound through the trigger slot).
 */
const togglePopover = (event: Event, toggle: () => void) => {
  void event;
  if (props.sessionId) void loadTaskRuns(props.sessionId);
  toggle();
};

/** One line per active run/command for the hover tooltip. */
const tooltip = computed(() => {
  const header = t('tasksButton.label');
  const lines = [
    ...activeRuns.value.map(run => `${runName(run)} · ${formatElapsed(run.execution?.started_at, now.value)}`),
    ...commands.value.map(command => `${command.summary} · ${formatElapsed(command.startedAtMs, now.value)}`)
  ];
  return lines.length ? [header, ...lines].join('\n') : `${header} · ${t('tasksButton.idle')}`;
});

/**
 * Display name of a run: its label, task name or the first line of the task.
 * @param run
 */
const runName = (run: SubagentRun): string =>
  run.label || run.task_name || (run.task ?? '').split('\n')[0]?.slice(0, 40) || run.run_id;

/**
 * Status chip text (queued / running / interrupted / unknown).
 * @param run
 */
const statusLabel = (run: SubagentRun): string => {
  const status = String(run.execution?.status ?? '').toUpperCase();
  if (status === 'PENDING') return t('tasksButton.queued');
  if (status === 'RUNNING') return t('tasksButton.running');
  if (status === 'INTERRUPTED') return t('tasksButton.interrupted');
  return status || t('tasksButton.unknown');
};

/**
 * Status chip colours: queued is neutral, running is the theme accent,
 * interrupted is amber (recoverable, not finished).
 * @param run
 */
const statusClass = (run: SubagentRun): string => {
  const status = String(run.execution?.status ?? '').toUpperCase();
  if (status === 'RUNNING') return 'bg-theme-main/10 text-theme-main';
  if (status === 'INTERRUPTED') return 'bg-amber-500/10 text-amber-600 dark:text-amber-400';
  return 'bg-gray-500/10 text-gray-500 dark:text-gray-400';
};

/**
 * Ask the session page to open a sub-agent run's live context view.
 * @param runId
 */
const openRun = (runId: string) => {
  emit('focus', { kind: 'run', id: runId });
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
      "queued": "排队中",
      "running": "运行中",
      "interrupted": "已中断",
      "unknown": "未知"
    }
  },
  "en": {
    "tasksButton": {
      "label": "Running background tasks",
      "idle": "Nothing running right now",
      "subagents": "Running sub-agents ({count})",
      "commands": "Running commands ({count})",
      "none": "None",
      "queued": "Queued",
      "running": "Running",
      "interrupted": "Interrupted",
      "unknown": "Unknown"
    }
  },
  "ja": {
    "tasksButton": {
      "label": "実行中のバックグラウンドタスク",
      "idle": "現在実行中のタスクはありません",
      "subagents": "実行中のサブエージェント（{count}）",
      "commands": "実行中のコマンド（{count}）",
      "none": "なし",
      "queued": "待機中",
      "running": "実行中",
      "interrupted": "中断",
      "unknown": "不明"
    }
  },
  "ko": {
    "tasksButton": {
      "label": "실행 중인 백그라운드 작업",
      "idle": "현재 실행 중인 작업이 없습니다",
      "subagents": "실행 중인 서브에이전트({count})",
      "commands": "실행 중인 명령({count})",
      "none": "없음",
      "queued": "대기 중",
      "running": "실행 중",
      "interrupted": "중단됨",
      "unknown": "알 수 없음"
    }
  }
}
</i18n>
