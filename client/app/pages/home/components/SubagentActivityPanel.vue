<template>
  <div
    class="flex h-full min-h-0 flex-col"
    data-test="subagent-activity-panel">
    <div
      v-if="!run"
      class="m-auto flex flex-col items-center gap-1 px-6 text-center text-xs text-gray-400 dark:text-gray-500"
      data-test="subagent-activity-empty">
      <i class="pi pi-android text-lg" />
      <span>{{ t('subagentActivity.noRun') }}</span>
    </div>
    <template v-else>
      <!-- Header: identity, status and elapsed time for this run. -->
      <div
        class="flex shrink-0 flex-col gap-1 border-b border-solid border-gray-100 px-3 py-2 dark:border-gray-800"
        data-test="subagent-activity-header">
        <div class="flex items-center gap-2">
          <i class="pi pi-android text-sm text-theme-main" />
          <span class="min-w-0 flex-1 truncate text-sm font-semibold text-gray-700 dark:text-gray-200">
            {{ runTitle }}
          </span>
          <span
            class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium"
            :class="badgeClass(run)"
            data-test="subagent-activity-status">
            {{ statusLabel(run) }}
          </span>
          <span
            class="shrink-0 font-mono text-[11px] text-gray-400"
            data-test="subagent-activity-elapsed"
            :title="t('subagentActivity.elapsed')">
            {{ elapsedText }}
          </span>
        </div>
        <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-6 text-[10px] text-gray-400 dark:text-gray-500">
          <span>{{ roleLabel(run) }}</span>
          <span>depth: {{ run.depth ?? 0 }}</span>
          <span class="truncate font-mono">{{ run.agent_id || run.run_id }}</span>
        </div>
      </div>

      <!-- Tabs: live timeline / run summary / steer instructions. -->
      <div
        class="flex shrink-0 items-center gap-1 border-b border-solid border-gray-100 px-2 py-1 dark:border-gray-800">
        <button
          v-for="tab in TABS"
          :key="tab"
          type="button"
          class="cursor-pointer rounded-full px-2.5 py-0.5 text-xs transition-colors"
          :class="
            activeTab === tab
              ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
          "
          :data-test="`subagent-activity-tab-${tab}`"
          @click="activeTab = tab">
          {{ t(`subagentActivity.${tab}`) }}
        </button>
      </div>

      <!-- Live: the child session's trajectory timeline. -->
      <div
        v-if="activeTab === 'live'"
        class="min-h-0 flex-1 overflow-hidden"
        data-test="subagent-activity-live">
        <SubagentTrajectory :session-id="childSessionKey" />
      </div>

      <!-- Summary: the run's own record. -->
      <div
        v-else-if="activeTab === 'summary'"
        class="min-h-0 flex-1 overflow-y-auto p-3"
        data-test="subagent-activity-summary">
        <dl class="m-0 flex flex-col gap-1.5 text-xs">
          <div
            v-for="row in summaryRows"
            :key="row.label"
            class="flex gap-2">
            <dt class="w-24 shrink-0 text-gray-400 dark:text-gray-500">{{ row.label }}</dt>
            <dd
              class="m-0 min-w-0 flex-1 break-words text-gray-700 dark:text-gray-200"
              :class="row.mono ? 'font-mono' : ''">
              {{ row.value }}
            </dd>
          </div>
        </dl>
        <div class="mt-3 flex flex-col gap-1">
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('subagentActivity.task') }}</span>
          <p class="m-0 whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-200">{{ run.task || '—' }}</p>
        </div>
        <div class="mt-3 flex flex-col gap-1">
          <span class="text-xs text-gray-400 dark:text-gray-500">{{ t('subagentActivity.result') }}</span>
          <pre
            class="m-0 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-2 text-[11px] text-gray-700 dark:bg-gray-800 dark:text-gray-200"
            data-test="subagent-activity-result"
            >{{ resultText }}</pre>
        </div>
      </div>

      <!-- Steer: send new instructions to a live run. -->
      <div
        v-else
        class="flex min-h-0 flex-1 flex-col gap-2 p-3"
        data-test="subagent-activity-steer">
        <textarea
          v-model="steerDraft"
          rows="4"
          class="w-full resize-y rounded-lg border border-solid border-gray-light bg-transparent px-3 py-2 text-sm outline-none focus:border-theme-main disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-dark"
          :placeholder="t('subagentActivity.steerPlaceholder')"
          :disabled="!isSteerable || steering"
          data-test="subagent-activity-steer-input" />
        <p
          v-if="!isSteerable"
          class="m-0 text-[11px] text-amber-600 dark:text-amber-400"
          data-test="subagent-activity-steer-disabled">
          {{ t('subagentActivity.steerDisabled') }}
        </p>
        <div class="flex justify-end">
          <Button
            :label="t('subagentActivity.steerSend')"
            size="small"
            :loading="steering"
            :disabled="!isSteerable || steering || !steerDraft.trim()"
            data-test="subagent-activity-steer-send"
            @click="sendSteer" />
        </div>
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import dayjs from 'dayjs';
import SubagentTrajectory from './SubagentTrajectory.vue';
import { formatElapsed } from '~/common/utils';
import { isRunning } from '~/utils/subagent';

const props = defineProps<{
  /** The tab payload: `{ runId }` for this activity tab. */
  payload?: { runId?: string };
}>();

const { t } = useI18n({ useScope: 'local' });

const TABS = ['live', 'summary', 'steer'] as const;
type ActivityTab = (typeof TABS)[number];

const runId = computed<string | null>(() => props.payload?.runId ?? null);
const { run, childSessionKey, isSteerable, steer } = useSubagentActivity(runId);
const { badgeClass, statusLabel, roleLabel } = useSubagentTasks();

/** Which tab is on screen (the live timeline is the point of this panel). */
const activeTab = ref<ActivityTab>('live');
const steerDraft = ref('');
const steering = ref(false);

/** Ticking clock for the elapsed header (only while the run is alive). */
const now = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | undefined;

const syncTicker = (): void => {
  const wanted = !!run.value && isRunning(run.value);
  if (wanted && !ticker) {
    ticker = setInterval(() => {
      now.value = Date.now();
    }, 1000);
  } else if (!wanted && ticker) {
    clearInterval(ticker);
    ticker = undefined;
  }
};
watch(() => (run.value ? isRunning(run.value) : false), syncTicker, { immediate: true });

/** Point the shared store at this run while the tab is open. */
watch(runId, id => useSubagentStore().setActiveActivityRun(id), { immediate: true });
onBeforeUnmount(() => {
  useSubagentStore().setActiveActivityRun(null);
  if (ticker) clearInterval(ticker);
});

/** Title: the run's label / task name, else the first task line. */
const runTitle = computed<string>(() => {
  const current = run.value;
  if (!current) return '';
  return current.label || current.task_name || (current.task ?? '').split('\n')[0]?.slice(0, 60) || current.run_id;
});

/** Elapsed for a live run; total duration for a finished one. */
const elapsedText = computed<string>(() => {
  const current = run.value;
  if (!current) return '';
  const started = current.execution.started_at;
  const ended = current.execution.ended_at;
  if (ended != null && started != null) return formatElapsed(started, Number(ended));
  return formatElapsed(started, now.value);
});

/**
 * Outcome / reasons read as an em dash when absent.
 * @param value
 */
const orDash = (value: unknown): string => {
  const text = value == null ? '' : String(value);
  return text || '—';
};

/**
 * Epoch ms → a readable local timestamp.
 * @param value
 */
const formatTime = (value: number | null | undefined): string =>
  value == null ? '—' : dayjs(Number(value)).format('YYYY-MM-DD HH:mm:ss');

/** The summary rows, in reading order. */
const summaryRows = computed<Array<{ label: string; value: string; mono?: boolean }>>(() => {
  const current = run.value;
  if (!current) return [];
  return [
    { label: t('subagentActivity.status'), value: statusLabel(current) },
    { label: t('subagentActivity.role'), value: roleLabel(current) },
    { label: t('subagentActivity.depth'), value: String(current.depth ?? 0) },
    { label: t('subagentActivity.agentId'), value: orDash(current.agent_id), mono: true },
    { label: t('subagentActivity.parent'), value: current.requester_session_key || '—', mono: true },
    { label: t('subagentActivity.started'), value: formatTime(current.execution.started_at) },
    { label: t('subagentActivity.ended'), value: formatTime(current.execution.ended_at) },
    { label: t('subagentActivity.outcome'), value: orDash(current.execution.outcome?.status) },
    { label: t('subagentActivity.endedReason'), value: orDash(current.ended_reason) },
    { label: t('subagentActivity.pauseReason'), value: orDash(current.pause_reason) },
    { label: t('subagentActivity.delivery'), value: orDash(current.delivery?.status) }
  ];
});

/** The completion result, bounded (the panel is a reader, not a dump). */
const resultText = computed<string>(() => {
  const text = run.value?.completion?.result_text ?? '';
  if (!text) return '—';
  return text.length > 4000 ? `${text.slice(0, 4000)}…` : text;
});

/** Send the typed instructions to the child agent. */
const sendSteer = async (): Promise<void> => {
  const text = steerDraft.value.trim();
  if (!text || !isSteerable.value || steering.value) return;
  steering.value = true;
  try {
    const ok = await steer(text);
    if (ok) {
      steerDraft.value = '';
      toastSuccess(t('subagentActivity.steerSent'));
    } else {
      toastError(t('subagentActivity.steerFailed'));
    }
  } catch {
    toastError(t('subagentActivity.steerFailed'));
  } finally {
    steering.value = false;
  }
};
</script>

<i18n lang="json">
{
  "zh": {
    "subagentActivity": {
      "noRun": "未选择智能体（该运行可能已被清理）。",
      "live": "实况",
      "summary": "摘要",
      "steer": "指令",
      "elapsed": "运行时间",
      "status": "状态",
      "role": "角色",
      "depth": "深度",
      "agentId": "代理 ID",
      "parent": "调用会话",
      "started": "开始时间",
      "ended": "结束时间",
      "outcome": "结果",
      "endedReason": "结束原因",
      "pauseReason": "暂停原因",
      "delivery": "投递状态",
      "task": "任务描述",
      "result": "返回结果",
      "steerPlaceholder": "输入新指令…",
      "steerSend": "发送并继续",
      "steerDisabled": "此代理已结束，无法发送指令",
      "steerSent": "指令已发送",
      "steerFailed": "指令发送失败"
    }
  },
  "en": {
    "subagentActivity": {
      "noRun": "No agent selected (this run may have been cleaned up).",
      "live": "Live",
      "summary": "Summary",
      "steer": "Steer",
      "elapsed": "Elapsed",
      "status": "Status",
      "role": "Role",
      "depth": "Depth",
      "agentId": "Agent ID",
      "parent": "Parent session",
      "started": "Started",
      "ended": "Ended",
      "outcome": "Outcome",
      "endedReason": "Ended reason",
      "pauseReason": "Pause reason",
      "delivery": "Delivery",
      "task": "Task",
      "result": "Result",
      "steerPlaceholder": "Enter new instructions…",
      "steerSend": "Send & Continue",
      "steerDisabled": "This agent has ended, steering is disabled",
      "steerSent": "Instructions sent",
      "steerFailed": "Could not send the instructions"
    }
  },
  "ja": {
    "subagentActivity": {
      "noRun": "エージェントが選択されていません（実行が削除された可能性があります）。",
      "live": "実況",
      "summary": "概要",
      "steer": "指示",
      "elapsed": "実行時間",
      "status": "状態",
      "role": "ロール",
      "depth": "深度",
      "agentId": "エージェント ID",
      "parent": "呼び出し元セッション",
      "started": "開始時刻",
      "ended": "終了時刻",
      "outcome": "結果",
      "endedReason": "終了理由",
      "pauseReason": "一時停止理由",
      "delivery": "配信状態",
      "task": "タスク",
      "result": "返却結果",
      "steerPlaceholder": "新しい指示を入力…",
      "steerSend": "送信して続行",
      "steerDisabled": "このエージェントは終了しているため、指示を送信できません",
      "steerSent": "指示を送信しました",
      "steerFailed": "指示を送信できませんでした"
    }
  },
  "ko": {
    "subagentActivity": {
      "noRun": "선택된 에이전트가 없습니다(실행이 정리되었을 수 있습니다).",
      "live": "실시간",
      "summary": "요약",
      "steer": "지시",
      "elapsed": "실행 시간",
      "status": "상태",
      "role": "역할",
      "depth": "깊이",
      "agentId": "에이전트 ID",
      "parent": "호출 세션",
      "started": "시작 시간",
      "ended": "종료 시간",
      "outcome": "결과",
      "endedReason": "종료 사유",
      "pauseReason": "일시정지 사유",
      "delivery": "전달 상태",
      "task": "작업 설명",
      "result": "반환 결과",
      "steerPlaceholder": "새 지시를 입력하세요…",
      "steerSend": "보내고 계속",
      "steerDisabled": "이 에이전트는 종료되어 지시를 보낼 수 없습니다",
      "steerSent": "지시를 보냈습니다",
      "steerFailed": "지시를 보내지 못했습니다"
    }
  }
}
</i18n>
