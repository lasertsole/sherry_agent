<template>
  <!-- Floating progress read-out: top-right of the chat list, collapsed to a pill
       by default. Both halves are pushed over the WebSocket (todo payloads and
       taskflow wave payloads), so it needs no polling and no navigation. -->
  <div
    v-if="visible"
    data-test="progress-float"
    class="pointer-events-auto absolute top-3 right-3 z-20 flex flex-col items-end">
    <button
      type="button"
      data-test="progress-float-trigger"
      class="flex items-center gap-2 rounded-full border border-solid border-gray-200/80 bg-white/90 px-2.5 py-1 text-xs shadow-sm backdrop-blur transition-colors hover:bg-white dark:border-gray-700/80 dark:bg-[#131619]/90 dark:hover:bg-[#181c20]"
      :title="expanded ? t('progressFloat.collapse') : t('progressFloat.expand')"
      :aria-label="expanded ? t('progressFloat.collapse') : t('progressFloat.expand')"
      :aria-expanded="expanded"
      @click="expanded = !expanded">
      <i
        class="pi pi-chart-line text-[11px] text-theme-main"
        aria-hidden="true"></i>
      <span data-test="progress-float-summary">{{ summaryText }}</span>
      <span
        v-if="store.totals.waves > 0"
        class="text-[11px] text-gray-500 dark:text-gray-400"
        data-test="progress-float-waves">
        {{ waveChipLabel }}
      </span>
      <i
        :class="['pi text-[10px] text-gray-400', expanded ? 'pi-chevron-up' : 'pi-chevron-down']"
        aria-hidden="true"></i>
    </button>

    <div
      v-if="expanded"
      data-test="progress-float-panel"
      class="mt-1.5 w-80 max-h-[60vh] overflow-y-auto rounded-lg border border-solid border-gray-200/80 bg-white/95 p-2.5 text-xs shadow-lg backdrop-blur dark:border-gray-700/80 dark:bg-[#131619]/95">
      <!-- Todo list (its own pushed payload) -->
      <section
        v-if="todoStore.todos.length"
        data-test="progress-float-todos">
        <header class="mb-1 flex items-center justify-between text-[11px] font-medium text-gray-500 dark:text-gray-400">
          <span class="flex items-center gap-1.5">
            <i
              class="pi pi-list text-[11px]"
              aria-hidden="true"></i>
            {{ t('progressFloat.todolist') }}
          </span>
          <span>{{ t('progressFloat.counts', { done: todoStore.doneCount, total: todoStore.todos.length }) }}</span>
        </header>
        <ul class="m-0 flex list-none flex-col gap-1 p-0">
          <li
            v-for="(todo, index) in todoStore.todos"
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
      </section>

      <!-- TaskFlow waves (the taskflow payload) -->
      <section
        v-for="flow in store.flows"
        :key="flow.flow_id"
        class="mt-2 border-t border-solid border-gray-100 pt-2 first:mt-0 first:border-t-0 first:pt-0 dark:border-gray-800"
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
        <div
          v-for="wave in flow.waves"
          :key="wave.index"
          class="mb-1.5 last:mb-0"
          data-test="progress-wave">
          <div class="flex items-center justify-between gap-2 text-[10px] text-gray-500 dark:text-gray-400">
            <span class="flex items-center gap-1">
              {{ t('progressFloat.waveLabel', { index: wave.index }) }}
              <span
                v-if="store.started && wave.index === flow.current_wave"
                class="rounded bg-theme-main/10 px-1 text-[9px] text-theme-main"
                data-test="progress-wave-current">
                {{ t('progressFloat.current') }}
              </span>
              <span
                v-if="wave.cyclic"
                class="rounded bg-amber-100 px-1 text-[9px] text-amber-600 dark:bg-amber-900/30 dark:text-amber-400"
                data-test="progress-wave-cyclic">
                {{ t('progressFloat.cyclic') }}
              </span>
            </span>
            <span>{{ wave.done }}/{{ wave.total }}</span>
          </div>
          <div class="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
            <div
              class="h-full rounded-full bg-emerald-500 transition-all"
              :style="{ width: `${wavePercent(wave)}%` }"></div>
          </div>
          <ul class="m-0 mt-1 flex list-none flex-col gap-0.5 p-0">
            <li
              v-for="step in wave.steps"
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
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useTaskflowStore } from '~/stores/taskflow';
import type { FlowWave } from '~/stores/taskflow';
import { useTodoStore } from '~/stores/todo';

const { t } = useI18n();
/** Wave/step progress (taskflow payloads). */
const store = useTaskflowStore();
store.subscribe();
/** The todo list half — a separate pushed payload, shown in the same box. */
const todoStore = useTodoStore();
todoStore.subscribe();

/** Collapsed by default: the pill is the resting state (it is an overlay). */
const expanded = ref(false);

/** Show the box only when one of the two halves has something to report. */
const visible = computed(() => store.hasProgress || todoStore.todos.length > 0);

/** Aggregate line inside the pill: steps done across every flow. */
const summaryText = computed(() => t('progressFloat.summary', { done: store.totals.done, total: store.totals.total }));

/** "wave X of Y" for the pill: X = the first wave with open work. */
/**
 * The pill's wave chip, in three honest states instead of one misleading number:
 *
 * - nothing started yet (every step merely ``ready``) → "not started": a board
 *   nobody has begun must not read as "wave 1/1" (it looks like work in flight);
 * - work under way → "wave X of Y" (X = the first wave with open work);
 * - every step settled while the flow is still open → "needs closing" (the plan
 *   gate asks the agent to close it; a wave number would be a lie until then).
 */
const waveChipLabel = computed(() => {
  if (!store.started) return t('progressFloat.notStarted');
  if (store.totals.current_wave > 0) {
    return t('progressFloat.waveSummary', {
      current: store.totals.current_wave,
      total: store.totals.waves
    });
  }
  return t('progressFloat.needsClosing');
});

/**
 * Terminal statuses (mirrors the backend vocabulary).
 * @param status
 */
function isTerminal(status: string): boolean {
  return status === 'done' || status === 'cancelled' || status === 'skipped';
}

/**
 * Status → glyph + tone (the same vocabulary the todo dock uses).
 * @param status
 */
function todoIcon(status: string): string {
  switch (status) {
    case 'done':
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

/**
 * Percentage of finished steps in one wave.
 * @param wave
 */
function wavePercent(wave: FlowWave): number {
  return wave.total > 0 ? Math.round((wave.done / wave.total) * 100) : 0;
}
</script>
