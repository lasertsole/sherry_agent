<template>
  <div
    class="flex h-full min-h-0 flex-col gap-2 p-3"
    data-test="taskflow-board">
    <!-- Overall progress across every active flow. -->
    <header class="flex shrink-0 flex-col gap-1">
      <div class="flex items-baseline gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">{{ t('taskBoard.title') }}</span>
        <span class="text-[11px] text-gray-400 dark:text-gray-500">
          {{ t('taskBoard.flows', { n: totals.flows }) }}
        </span>
        <span
          class="ml-auto font-mono text-[11px] text-gray-500 dark:text-gray-400"
          data-test="taskboard-counts">
          {{ totals.done }}/{{ totals.total }}
        </span>
      </div>
      <div class="h-1.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
        <div
          class="h-full rounded-full bg-theme-main transition-all"
          :style="{ width: `${percent}%` }"
          data-test="taskboard-progress"
          :aria-label="t('taskBoard.percent', { percent })" />
      </div>
    </header>

    <p
      v-if="!flows.length"
      class="m-auto text-xs text-gray-400 dark:text-gray-500"
      data-test="taskboard-empty">
      {{ t('taskBoard.empty') }}
    </p>

    <div
      v-else
      class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
      <section
        v-for="flow in flows"
        :key="flow.flow_id"
        class="flex flex-col gap-2 rounded-lg border border-solid border-gray-100 p-2 dark:border-gray-800"
        :data-test="`taskboard-flow-${flow.flow_id}`">
        <!-- Flow header: description, status, step count. -->
        <div class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span class="min-w-0 flex-1 break-words text-xs font-medium text-gray-700 dark:text-gray-200">
            {{ flow.description || flow.flow_id }}
          </span>
          <span
            class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium"
            :class="flowStatusClass(flow.status)">
            {{ flow.status }}
          </span>
          <span class="shrink-0 font-mono text-[10px] text-gray-400">
            {{ t('taskBoard.steps', { done: flow.done, total: flow.total }) }}
          </span>
        </div>

        <!-- Swimlanes: one column per non-empty status, in lifecycle order. -->
        <div class="flex flex-wrap items-start gap-2">
          <div
            v-for="lane in lanesOf(flow)"
            :key="lane.status"
            class="flex min-w-[9rem] flex-1 flex-col gap-1 rounded-lg bg-gray-50/70 p-1.5 dark:bg-gray-900/40"
            :data-test="`taskboard-lane-${lane.status}`">
            <span class="flex items-center gap-1 text-[10px] font-medium text-gray-500 dark:text-gray-400">
              <i
                :class="['pi text-[9px]', statusIcon(lane.status)]"
                aria-hidden="true" />
              {{ t(`progressFloat.status.${lane.status}`) }}
              <span class="ml-auto font-mono">{{ lane.steps.length }}</span>
            </span>
            <div
              v-for="step in lane.steps"
              :key="step.step_id"
              class="flex flex-col gap-0.5 rounded-md border border-solid px-2 py-1"
              :class="stepClass(step.status)"
              :data-test="`taskboard-step-${step.step_id}`">
              <div class="flex items-center gap-1">
                <i
                  :class="['pi text-[10px]', statusIcon(step.status)]"
                  :data-test="`taskboard-step-icon-${step.step_id}`"
                  aria-hidden="true" />
                <span class="truncate font-mono text-[10px] text-gray-400">{{ step.step_id }}</span>
              </div>
              <span
                class="break-words text-[11px] text-gray-700 dark:text-gray-200"
                :class="isTerminal(step.status) ? 'text-gray-400 line-through dark:text-gray-500' : ''"
                :title="step.task">
                {{ clip(step.task) }}
              </span>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { FlowProgress, FlowStep } from '~/stores/taskflow';

const { t } = useI18n({ useScope: 'local' });

const store = useTaskflowStore();
const flows = computed<FlowProgress[]>(() => store.flows ?? []);
const totals = computed(() => store.totals ?? { flows: 0, total: 0, done: 0 });
const percent = computed(() => store.percent);

/** Lifecycle order of the lanes (only non-empty ones render). */
const STATUS_ORDER = ['blocked', 'ready', 'dispatched', 'done', 'failed', 'skipped', 'cancelled'] as const;

/**
 * Group one flow's steps into lanes, in lifecycle order.
 * @param flow
 */
const lanesOf = (flow: FlowProgress): Array<{ status: string; steps: FlowStep[] }> => {
  const known = new Set<string>(STATUS_ORDER);
  const lanes: Array<{ status: string; steps: FlowStep[] }> = STATUS_ORDER.map(status => ({
    status,
    steps: (flow.steps ?? []).filter(step => step.status === status)
  }));
  // An unknown status still renders (at the end) instead of disappearing.
  const extras = new Map<string, FlowStep[]>();
  for (const step of flow.steps ?? []) {
    if (known.has(step.status)) continue;
    const bucket = extras.get(step.status) ?? [];
    bucket.push(step);
    extras.set(step.status, bucket);
  }
  for (const [status, steps] of extras) lanes.push({ status, steps });
  return lanes.filter(lane => lane.steps.length > 0);
};

/**
 * Terminal statuses render struck-through.
 * @param status
 */
const isTerminal = (status: string): boolean =>
  status === 'done' || status === 'skipped' || status === 'cancelled' || status === 'failed';

/**
 * A step's card tint / border by status.
 * @param status
 */
const stepClass = (status: string): string => {
  if (status === 'dispatched') return 'border-theme-main bg-theme-main/5';
  if (status === 'failed') return 'border-red-300 bg-red-50/60 dark:border-red-900/70 dark:bg-red-950/20';
  if (status === 'done') return 'border-emerald-200 bg-emerald-50/50 dark:border-emerald-900/50 dark:bg-emerald-950/20';
  return 'border-gray-200 dark:border-gray-700';
};

/**
 * Status glyph (a spinner while a child agent is on the step).
 * @param status
 */
const statusIcon = (status: string): string => {
  if (status === 'dispatched') return 'pi-spin pi-spinner text-theme-main';
  if (status === 'done') return 'pi-check-circle text-emerald-500';
  if (status === 'failed') return 'pi-times-circle text-red-500';
  if (status === 'ready') return 'pi-circle text-blue-500';
  if (status === 'blocked') return 'pi-lock text-gray-400';
  return 'pi-minus-circle text-gray-400';
};

/**
 * The flow-level status chip tint.
 * @param status
 */
const flowStatusClass = (status: string): string => {
  const value = status.toUpperCase();
  if (value === 'RUNNING' || value === 'DISPATCHED') return 'bg-theme-main/10 text-theme-main';
  if (value === 'COMPLETED' || value === 'DONE')
    return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300';
  if (value === 'FAILED' || value === 'CANCELLED')
    return 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300';
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';
};

/**
 * The task text, bounded to one readable card's worth.
 * @param text
 */
const clip = (text: string): string => {
  const value = text ?? '';
  return value.length > 200 ? `${value.slice(0, 200)}…` : value;
};
</script>

<i18n lang="json">
{
  "zh": {
    "taskBoard": {
      "title": "任务看板",
      "flows": "{n} 个任务流",
      "steps": "{done}/{total} 步",
      "percent": "总进度 {percent}%",
      "empty": "暂无进行中的任务流。"
    }
  },
  "en": {
    "taskBoard": {
      "title": "Task Board",
      "flows": "{n} flows",
      "steps": "{done}/{total} steps",
      "percent": "{percent}% complete",
      "empty": "No active task flows."
    }
  },
  "ja": {
    "taskBoard": {
      "title": "タスクボード",
      "flows": "{n} フロー",
      "steps": "{done}/{total} ステップ",
      "percent": "全体 {percent}%",
      "empty": "進行中のタスクフローはありません。"
    }
  },
  "ko": {
    "taskBoard": {
      "title": "작업 보드",
      "flows": "{n}개 흐름",
      "steps": "{done}/{total} 단계",
      "percent": "전체 {percent}%",
      "empty": "진행 중인 작업 흐름이 없습니다."
    }
  }
}
</i18n>
