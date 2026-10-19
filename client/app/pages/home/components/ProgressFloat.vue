<template>
  <!-- Floating progress read-out: top-right of the chat list, collapsed to a pill
       by default. ONE plan from two pushed payloads (the todo rows and the
       taskflow step lists over the WebSocket), so it needs no polling and no
       navigation. It is PERMANENT — it stays on screen with no plan at all (the
       panel then shows the empty state), so the control never disappears. -->
  <div
    data-test="progress-float"
    class="pointer-events-auto absolute top-3 right-3 z-20 flex flex-col items-end">
    <!-- Collapsed = quite transparent (alpha .3 — the chat shows through it); expanded
         = fully opaque. The background alpha animates over 0.3 s in both directions
         (`transition-colors duration-300`; the panel below is opaque by itself). -->
    <button
      type="button"
      data-test="progress-float-trigger"
      class="flex items-center gap-2 rounded-full border border-solid border-gray-200/80 px-2.5 py-1 text-xs shadow-sm backdrop-blur transition-colors duration-300 hover:bg-white dark:border-gray-700/80 dark:hover:bg-[#181c20]"
      :class="expanded ? 'bg-white dark:bg-[#131619]' : 'bg-white/30 dark:bg-[#131619]/30'"
      :title="expanded ? t('progressFloat.collapse') : t('progressFloat.expand')"
      :aria-label="expanded ? t('progressFloat.collapse') : t('progressFloat.expand')"
      :aria-expanded="expanded"
      @click="expanded = !expanded">
      <i
        :class="['pi pi-chart-line text-[11px]', hasPlan ? 'text-theme-main' : 'text-gray-400']"
        aria-hidden="true"></i>
      <span
        data-test="progress-float-summary"
        :class="hasPlan ? '' : 'text-gray-400 dark:text-gray-500'"
        >{{ summaryText }}</span
      >
      <span
        v-if="store.totals.total > 0"
        class="text-[11px] text-gray-500 dark:text-gray-400"
        data-test="progress-float-steps">
        {{ doneChipLabel }}
      </span>
      <i
        :class="['pi text-[10px] text-gray-400', expanded ? 'pi-chevron-up' : 'pi-chevron-down']"
        aria-hidden="true"></i>
    </button>

    <div
      v-if="expanded"
      data-test="progress-float-panel"
      class="mt-1.5 w-80 max-h-[60vh] overflow-y-auto rounded-lg border border-solid border-gray-200/80 bg-white p-2.5 text-xs shadow-lg backdrop-blur dark:border-gray-700/80 dark:bg-[#131619]">
      <!-- ONE box for ONE plan. The checklist rows and the TaskFlow steps are the
           same running plan (the backend's plan gate reads todos ∪ open flows), so
           they share a single header and a single list: a todo that mirrors a
           rendered step is filtered out and appears once, inside its flow. -->
      <header
        v-if="hasPlan"
        class="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-gray-500 dark:text-gray-400"
        data-test="progress-float-header">
        <i
          class="pi pi-list text-[11px]"
          aria-hidden="true"></i>
        {{ t('progressFloat.summary', { done: planDone, total: planTotal }) }}
      </header>

      <!-- Checklist rows that are not part of a tracked flow -->
      <ul
        v-if="planTodos.length"
        class="m-0 flex list-none flex-col gap-1 p-0"
        data-test="progress-float-todos">
        <li
          v-for="(todo, index) in planTodos"
          :key="`${todo.flow_id ?? ''}:${todo.step_id ?? ''}:${index}`"
          class="flex items-start gap-1.5"
          :data-test="'progress-todo'">
          <i
            :class="['pi mt-0.5 text-[11px]', todoIcon(todo.status)]"
            :title="t(`progressFloat.status.${todo.status}`)"
            aria-hidden="true"></i>
          <span
            class="min-w-0 flex-1 break-words"
            :class="isTerminal(todo.status) ? 'text-gray-400 line-through dark:text-gray-500' : ''">
            {{ todo.content }}
          </span>
          <span
            v-if="todo.flow_id"
            class="shrink-0 text-[10px] text-gray-400">
            {{ todo.flow_id }}
          </span>
        </li>
      </ul>

      <!-- TaskFlow flows: one caption per flow, then its flat step list -->
      <section
        v-for="(flow, flowIndex) in store.flows"
        :key="flow.flow_id"
        :class="[
          'border-solid border-gray-100 dark:border-gray-800',
          planTodos.length > 0 || flowIndex > 0 ? 'mt-2 border-t pt-2' : 'mt-2'
        ]"
        :data-test="'progress-flow'">
        <header class="mb-1 flex items-center justify-between text-[11px] font-medium text-gray-500 dark:text-gray-400">
          <span class="flex min-w-0 items-center gap-1.5">
            <i
              class="pi pi-sitemap text-[11px]"
              aria-hidden="true"></i>
            <span class="truncate">{{ flow.description || flow.flow_id }}</span>
          </span>
          <span class="shrink-0">{{ t('progressFloat.counts', { done: flow.done, total: flow.total }) }}</span>
        </header>
        <ul
          class="m-0 mt-1 flex list-none flex-col gap-0.5 p-0"
          data-test="progress-flow-steps">
          <li
            v-for="step in flow.steps"
            :key="step.step_id"
            class="flex items-start gap-1.5"
            data-test="progress-step">
            <i
              :class="['pi mt-0.5 text-[10px]', todoIcon(step.status)]"
              :title="t(`progressFloat.status.${step.status}`)"
              aria-hidden="true"></i>
            <span
              :class="['min-w-0 flex-1 truncate', isTerminal(step.status) ? 'text-gray-400 dark:text-gray-500' : '']">
              {{ step.task }}
            </span>
            <span class="shrink-0 text-[10px] text-gray-400">{{ step.step_id }}</span>
          </li>
        </ul>
      </section>

      <!-- No plan: the panel says so instead of opening as a bare box -->
      <p
        v-if="!hasPlan"
        class="m-0 text-[11px] text-gray-400 dark:text-gray-500"
        data-test="progress-float-empty">
        {{ t('progressFloat.empty') }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useTaskflowStore } from '~/stores/taskflow';
import { useTodoStore } from '~/stores/todo';

const { t } = useI18n();
/** Flow/step progress (taskflow payloads). */
const store = useTaskflowStore();
store.subscribe();
/** The checklist half of the same plan — its own pushed payload. */
const todoStore = useTodoStore();
todoStore.subscribe();

/** Collapsed by default: the pill is the resting state (it is an overlay). */
const expanded = ref(false);

/**
 * Whether the plan has anything to report. The FLOAT is permanent (it stays on
 * screen with no plan — 始终显示), so this only switches the panel body between
 * the plan view and the empty-state line.
 */
const hasPlan = computed(() => store.hasProgress || todoStore.todos.length > 0);

/**
 * Steps already rendered inside a flow block, keyed `flow:step` — the dedupe set.
 * A todo carrying the same pair is that step, not a second item.
 */
const renderedSteps = computed(() => {
  const keys = new Set<string>();
  for (const flow of store.flows) {
    for (const step of flow.steps) keys.add(`${flow.flow_id}:${step.step_id}`);
  }
  return keys;
});

/**
 * Checklist rows shown on their own: todos that are NOT a rendered flow step
 * (a linked todo would otherwise appear twice — once here, once in its flow).
 */
const planTodos = computed(() =>
  todoStore.todos.filter(
    todo => !(todo.flow_id && todo.step_id && renderedSteps.value.has(`${todo.flow_id}:${todo.step_id}`))
  )
);

/**
 * Plan-level numbers: every checklist row plus every flow step, counted once —
 * the same "todos ∪ open flows" span the backend's plan gate reads.
 */
const planDone = computed(() => planTodos.value.filter(todo => isTerminal(todo.status)).length + store.totals.done);
const planTotal = computed(() => planTodos.value.length + store.totals.total);

/** Aggregate line inside the pill — the same numbers as the expanded header. */
const summaryText = computed(() => t('progressFloat.summary', { done: planDone.value, total: planTotal.value }));

/**
 * The pill's step chip, in two honest states:
 *
 * - nothing started yet (every step merely ``ready``) → "not started": a board
 *   nobody has begun must not read as progress;
 * - work under way → "X / Y done" from the flat step totals.
 */
const doneChipLabel = computed(() => {
  if (!store.started) return t('progressFloat.notStarted');
  return t('progressFloat.doneSummary', { done: store.totals.done, total: store.totals.total });
});

/**
 * Terminal statuses across BOTH vocabularies the box renders: flow steps
 * (done/cancelled/skipped) and checklist rows (completed/cancelled).
 * @param status
 * @returns True when the status counts as finished.
 */
function isTerminal(status: string): boolean {
  return status === 'done' || status === 'completed' || status === 'cancelled' || status === 'skipped';
}

/**
 * Status → glyph + tone (the same vocabulary the todo dock uses).
 *
 * BOTH vocabularies the box renders must map here: flow steps say ``done``,
 * checklist rows say ``completed`` — a missing case silently degrades that
 * whole surface to the grey circle (and hides the green tick).
 * @param status
 * @returns The `pi` icon classes for the status.
 */
function todoIcon(status: string): string {
  switch (status) {
    case 'done':
    case 'completed':
      return 'pi-check-circle text-emerald-500';
    case 'in_progress':
    case 'dispatched':
      return 'pi-spin pi-spinner text-sky-500';
    case 'blocked':
      return 'pi-ban text-amber-500';
    case 'failed':
      return 'pi-times-circle text-red-500';
    case 'skipped':
    case 'cancelled':
      return 'pi-minus-circle text-gray-400';
    default:
      return 'pi-circle text-gray-400';
  }
}
</script>
