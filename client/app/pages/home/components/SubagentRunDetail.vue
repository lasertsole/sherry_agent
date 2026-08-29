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

      <!-- 改道 / 恢复执行（仅 RUNNING / INTERRUPTED 状态可用） -->
      <div
        v-if="canSteer"
        class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
        <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
          {{ t('taskDetail.steerTitle') }}
        </div>
        <div class="text-xs text-gray-500 dark:text-gray-400 mb-2">
          {{ t('taskDetail.steerHint') }}
        </div>
        <textarea
          v-model="steerInput"
          :placeholder="t('taskDetail.steerPlaceholder')"
          rows="3"
          class="w-full rounded-md border border-solid border-gray-light dark:border-gray-dark bg-gray-50 dark:bg-[#23272b] px-3 py-2 text-sm text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-400 resize-none" />
        <div class="flex items-center gap-3 mt-2">
          <button
            :disabled="steering"
            class="px-3 py-1.5 rounded-md text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            @click="doSteer">
            {{ steering ? t('taskDetail.steerSteering') : t('taskDetail.steerAction') }}
          </button>
          <span
            v-if="steerFeedback"
            :class="['text-xs', steerOk ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400']">
            {{ steerFeedback }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import dayjs from 'dayjs';
import type { SubagentRun } from '@/composables/bridge';
import { steerSubagentRun } from '@/composables/bridge';
import { useSubagentTasks } from '@/composables/useSubagentTasks';

const props = defineProps<{
  /** 当前选中的运行记录；undefined 表示未选中任何节点 */
  run?: SubagentRun;
}>();

  const { t } = useI18n({ useScope: 'local' });
const { badgeClass, statusLabel, roleLabel, runLabel, parentSessionLabel, refreshFocusedSubtree } = useSubagentTasks();

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

// ---------------------------------------------------------------------------
// 改道 / 恢复执行：RUNNING / INTERRUPTED 的 run 可注入新指令并从 checkpointer 续跑
// ---------------------------------------------------------------------------
const steerInput = ref('');
const steering = ref(false);
const steerFeedback = ref('');
const steerOk = ref(false);

/** 仅执行中/已中断的 run 可改道（与后端 steer_subagent_run 的状态门槛一致）。
 *  注意：wire 上 ExecutionStatus 枚举值为小写（model_dump mode="json"），
 *  这里归一成大写再比较，兼容两种来源。 */
const canSteer = computed(() => {
  const status = (props.run?.execution?.status ?? '').toUpperCase();
  return status === 'RUNNING' || status === 'INTERRUPTED';
});

async function doSteer(): Promise<void> {
  const run = props.run;
  if (!run || steering.value) return;
  steering.value = true;
  steerFeedback.value = '';
  try {
    const instructions = steerInput.value.trim();
    const steered = await steerSubagentRun(
      run.run_id,
      instructions ? { new_instructions: instructions } : {}
    );
    if (steered) {
      steerOk.value = true;
      steerFeedback.value = t('taskDetail.steerSuccess');
      steerInput.value = '';
      // 后端已换代执行；拉取聚焦子树刷新整个任务面板（本组件的 run 由父级派生）
      await refreshFocusedSubtree();
    } else {
      steerOk.value = false;
      steerFeedback.value = t('taskDetail.steerFailed');
    }
  } finally {
    steering.value = false;
  }
}

/** 渲染执行时间：epoch 毫秒 → 本地可读字符串；空值显示占位符 '-' */
function formatTime(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(Number(ms))) return '-';
  return dayjs(Number(ms)).format('YYYY-MM-DD HH:mm:ss');
}
</script>

<i18n lang="json">
{
  "zh": {
    "taskDetail": {
      "placeholder": "点击上方树状图中的节点查看任务详情",
      "taskDesc": "任务描述",
      "status": "状态",
      "depth": "深度",
      "agentId": "Agent",
      "spawnMode": "生成方式",
      "parentSession": "调用方 Session",
      "startedAt": "开始时间",
      "endedAt": "结束时间",
      "endedReason": "结束原因",
      "outcome": "执行结果",
      "outcomeStatus": "结果状态",
      "error": "错误信息",
      "resultText": "返回内容",
      "delivery": "配送状态",
      "steerTitle": "改道 / 恢复执行",
      "steerHint": "发送后，子任务将带着当前对话上下文与你的新指令继续执行；留空则仅恢复被中断的任务。",
      "steerPlaceholder": "新指令（可选，例如：换个方向重试…）",
      "steerAction": "发送并继续",
      "steerSteering": "发送中…",
      "steerSuccess": "指令已送达，子任务正在继续。",
      "steerFailed": "改道失败（任务可能已结束或频率受限）"
    }
  },
  "en": {
    "taskDetail": {
      "placeholder": "Click a node in the tree above to view task details",
      "taskDesc": "Description",
      "status": "Status",
      "depth": "Depth",
      "agentId": "Agent",
      "spawnMode": "Spawn Mode",
      "parentSession": "Parent Session",
      "startedAt": "Started At",
      "endedAt": "Ended At",
      "endedReason": "End Reason",
      "outcome": "Outcome",
      "outcomeStatus": "Outcome Status",
      "error": "Error",
      "resultText": "Result",
      "delivery": "Delivery",
      "steerTitle": "Steer / Resume",
      "steerHint": "The child agent continues from its current context with your new instructions; leave empty to simply resume an interrupted run.",
      "steerPlaceholder": "New instructions (optional, e.g. retry with a different approach…)",
      "steerAction": "Send & Continue",
      "steerSteering": "Sending…",
      "steerSuccess": "Sent — the child agent is continuing.",
      "steerFailed": "Steer failed (run may have finished or is rate-limited)"
    }
  },
  "ja": {
    "taskDetail": {
      "placeholder": "上のツリーでノードをクリックするとタスク詳細を表示します",
      "taskDesc": "説明",
      "status": "ステータス",
      "depth": "深さ",
      "agentId": "エージェント",
      "spawnMode": "生成モード",
      "parentSession": "親セッション",
      "startedAt": "開始時刻",
      "endedAt": "終了時刻",
      "endedReason": "終了理由",
      "outcome": "結果",
      "outcomeStatus": "結果ステータス",
      "error": "エラー",
      "resultText": "結果",
      "delivery": "配信",
      "steerTitle": "軌道修正 / 再開",
      "steerHint": "送信すると、子エージェントは現在のコンテキストと新しい指示に従って処理を続けます。空欄の場合は中断されたタスクの再開のみ行います。",
      "steerPlaceholder": "新しい指示（任意。例：別のアプローチで再試行…）",
      "steerAction": "送信して続行",
      "steerSteering": "送信中…",
      "steerSuccess": "指示を送信しました。子エージェントが処理を続けています。",
      "steerFailed": "軌道修正に失敗しました（タスク終了済みまたはレート制限の可能性）"
    }
  },
  "ko": {
    "taskDetail": {
      "placeholder": "위 트리에서 노드를 클릭하면 작업 세부정보를 확인할 수 있습니다",
      "taskDesc": "설명",
      "status": "상태",
      "depth": "깊이",
      "agentId": "에이전트",
      "spawnMode": "생성 모드",
      "parentSession": "상위 세션",
      "startedAt": "시작 시간",
      "endedAt": "종료 시간",
      "endedReason": "종료 이유",
      "outcome": "결과",
      "outcomeStatus": "결과 상태",
      "error": "오류",
      "resultText": "결과",
      "delivery": "전송",
      "steerTitle": "경로 변경 / 재개",
      "steerHint": "보내면 하위 에이전트가 현재 컨텍스트와 새 지시에 따라 작업을 계속합니다. 비워 두면 중단된 작업만 재개합니다.",
      "steerPlaceholder": "새 지시(선택 사항, 예: 다른 접근으로 재시도…)",
      "steerAction": "보내고 계속하기",
      "steerSteering": "전송 중…",
      "steerSuccess": "지시를 전달했습니다. 하위 에이전트가 계속 진행합니다.",
      "steerFailed": "경로 변경 실패(작업이 이미 종료되었거나 빈도 제한일 수 있음)"
    }
  }
}
</i18n>
