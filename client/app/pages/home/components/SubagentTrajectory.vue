<template>
  <div
    class="flex h-full min-h-0 flex-col gap-2 p-3"
    data-test="subagent-trajectory">
    <div
      v-if="!sessionId"
      class="m-auto text-xs text-gray-400 dark:text-gray-500"
      data-test="subagent-trajectory-empty">
      {{ t('subagentActivity.noSession') }}
    </div>
    <template v-else>
      <p
        v-if="error"
        class="m-0 text-xs text-red-500"
        data-test="subagent-trajectory-error">
        {{ t('subagentActivity.liveError') }}
      </p>
      <p
        v-if="!turns.length && !error"
        class="m-auto text-xs text-gray-400 dark:text-gray-500"
        data-test="subagent-trajectory-empty">
        {{ t('subagentActivity.liveEmpty') }}
      </p>
      <div
        v-else
        ref="scroller"
        class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
        <section
          v-for="turn in turns"
          :key="turn.turnId"
          class="flex flex-col gap-1"
          :data-test="`subagent-turn-${turn.turnId}`">
          <div class="flex items-baseline justify-between gap-2">
            <span class="text-[11px] font-medium text-gray-500 dark:text-gray-400">
              {{ t('subagentActivity.turn', { id: shortTurn(turn.turnId) }) }}
            </span>
            <span class="font-mono text-[10px] text-gray-400">{{ formatClock(turn.events[0]!.ts) }}</span>
          </div>
          <div
            v-for="event in turn.events"
            :key="event.event_id"
            class="flex items-start gap-2 rounded-lg border border-solid border-gray-100 px-2 py-1.5 dark:border-gray-800"
            :class="event.is_error ? 'border-red-200 dark:border-red-900/60' : ''"
            :data-test="`subagent-event-${event.event_id}`">
            <i
              :class="[
                'pi mt-0.5 shrink-0 text-[10px]',
                eventIcon(event),
                event.is_error ? 'text-red-500' : 'text-gray-400'
              ]" />
            <div class="flex min-w-0 flex-1 flex-col gap-0.5">
              <div class="flex items-baseline gap-2">
                <span
                  class="min-w-0 flex-1 truncate text-xs font-medium text-gray-700 dark:text-gray-200"
                  :title="event.summary">
                  {{ event.summary }}
                </span>
                <span
                  v-if="event.payload.duration_ms != null"
                  class="shrink-0 font-mono text-[10px] text-gray-400">
                  {{ formatToolDuration(Number(event.payload.duration_ms)) }}
                </span>
                <span
                  v-else-if="isToolRow(event) && !event.is_error"
                  class="shrink-0 text-[10px] text-theme-main">
                  {{ t('subagentActivity.running') }}
                </span>
              </div>
              <span
                v-if="rowPreview(event)"
                class="truncate font-mono text-[10px] text-gray-400"
                :title="rowPreview(event)">
                {{ rowPreview(event) }}
              </span>
              <span
                v-if="event.kind === 'turn_end' && event.payload.total_ms != null"
                class="font-mono text-[10px] text-gray-400">
                {{ t('subagentActivity.turnTotal', { duration: formatDuration(Number(event.payload.total_ms)) }) }}
              </span>
            </div>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { TrajectoryEvent } from '~/composables/bridge/session';
import { formatToolDuration } from '~/common/utils';
// Nested bridge modules are not in the Nuxt auto-import set.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchTrajectory } from '~/composables/bridge/session';

const props = defineProps<{
  /** The child session whose ledger is shown (null before the child exists). */
  sessionId: string | null;
}>();

const { t } = useI18n({ useScope: 'local' });

const events = ref<TrajectoryEvent[]>([]);
const error = ref(false);
const scroller = ref<HTMLElement | null>(null);
let pollTimer: ReturnType<typeof setInterval> | undefined;
let loading = false;

/** Turn-grouped rows (the shared trajectory helpers). */
const turns = computed(() => groupTurns(events.value));

/**
 * Poll one page of the child session's ledger.
 *
 * HTTP polling is the documented bridge until the trajectory WS channel lands:
 * the payload/shape is identical, so switching the source later is a one-line
 * change. A failed read keeps the last good rows and flags the error.
 */
const load = async (): Promise<void> => {
  const sessionId = props.sessionId;
  if (!sessionId || loading) return;
  loading = true;
  try {
    const page = await fetchTrajectory(sessionId, { limit: 100 });
    if (page === null) {
      error.value = true;
      return;
    }
    error.value = false;
    const grew = page.length !== events.value.length;
    events.value = page;
    if (grew) {
      await nextTick();
      if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
    }
  } catch {
    error.value = true;
  } finally {
    loading = false;
  }
};

const stopPolling = (): void => {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined;
};

const startPolling = (): void => {
  stopPolling();
  if (!props.sessionId) return;
  pollTimer = setInterval(() => void load(), 3000);
};

watch(
  () => props.sessionId,
  () => {
    events.value = [];
    error.value = false;
    void load();
    startPolling();
  },
  { immediate: true }
);

onBeforeUnmount(stopPolling);

/**
 * Short turn label (turn ids are long).
 * @param turnId
 */
const shortTurn = (turnId: string): string => turnId.slice(0, 8);

/**
 * Whether the row is a tool call (it may still be running).
 * @param event
 */
const isToolRow = (event: TrajectoryEvent): boolean => event.kind === 'tool_start' || event.kind === 'tool_end';

/**
 * Row icon by kind: a spinner while a tool runs, a check/times for a finished
 * tool, and small glyphs for the lifecycle rows.
 * @param event
 */
const eventIcon = (event: TrajectoryEvent): string => {
  if (isToolRow(event)) {
    if (event.payload.duration_ms != null || event.is_error) {
      return event.is_error ? 'pi-times-circle' : 'pi-check-circle';
    }
    return 'pi-spin pi-spinner';
  }
  if (event.kind === 'turn_start') return 'pi-play-circle';
  if (event.kind === 'turn_end') return 'pi-flag';
  if (event.kind === 'model_retry') return 'pi-replay';
  if (event.kind === 'compact') return 'pi-compress';
  return event.is_error ? 'pi-exclamation-circle' : 'pi-info-circle';
};

/**
 * One clipped preview line for the row's args / error / payload.
 * @param event
 */
const rowPreview = (event: TrajectoryEvent): string => {
  if (isToolRow(event)) {
    if (event.payload.error && event.payload.result) return compactJson(event.payload.result, 140);
    if (event.payload.args) return compactJson(event.payload.args, 140);
    return '';
  }
  return previewOf(event);
};
</script>

<i18n lang="json">
{
  "zh": {
    "subagentActivity": {
      "noSession": "该子代理尚未建立子会话。",
      "liveEmpty": "暂无实况事件，等待第一个事件…",
      "liveError": "实况加载失败（将自动重试）。",
      "turn": "回合 {id}",
      "turnTotal": "耗时 {duration}",
      "running": "运行中"
    }
  },
  "en": {
    "subagentActivity": {
      "noSession": "This sub-agent has no child session yet.",
      "liveEmpty": "No activity yet — waiting for the first event…",
      "liveError": "Could not load the live activity (retrying automatically).",
      "turn": "Turn {id}",
      "turnTotal": "took {duration}",
      "running": "running"
    }
  },
  "ja": {
    "subagentActivity": {
      "noSession": "このサブエージェントにはまだ子セッションがありません。",
      "liveEmpty": "まだ実況イベントはありません（最初のイベントを待っています）…",
      "liveError": "実況を読み込めませんでした（自動で再試行します）。",
      "turn": "ターン {id}",
      "turnTotal": "所要 {duration}",
      "running": "実行中"
    }
  },
  "ko": {
    "subagentActivity": {
      "noSession": "이 서브에이전트에는 아직 자식 세션이 없습니다.",
      "liveEmpty": "아직 실시간 이벤트가 없습니다 — 첫 이벤트를 기다리는 중…",
      "liveError": "실시간 활동을 불러오지 못했습니다(자동으로 재시도합니다).",
      "turn": "턴 {id}",
      "turnTotal": "소요 {duration}",
      "running": "실행 중"
    }
  }
}
</i18n>
