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
        v-if="!connected && events.length"
        class="m-0 text-[11px] text-amber-600 dark:text-amber-400"
        data-test="subagent-trajectory-offline">
        {{ t('subagentActivity.offline') }}
      </p>
      <p
        v-if="!turns.length"
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
import { computed, nextTick, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { TrajectoryEvent } from '~/composables/bridge/session';
import { formatToolDuration } from '~/common/utils';

const props = defineProps<{
  /** The child session whose ledger is shown (null before the child exists). */
  sessionId: string | null;
}>();

const { t } = useI18n({ useScope: 'local' });

/** The live stream: ready snapshot + one frame per persisted event. */
const sessionRef = computed(() => props.sessionId ?? null);
const { events, connected } = useTrajectoryStream(sessionRef);

const scroller = ref<HTMLElement | null>(null);

/** Turn-grouped rows (the shared trajectory helpers). */
const turns = computed(() => groupTurns(events.value));

// Follow the tail as rows arrive (the live view is a log, not a document).
watch(
  () => events.value.length,
  async () => {
    await nextTick();
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
  }
);

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
      "offline": "实况连接已断开，正在自动重连…",
      "turn": "回合 {id}",
      "turnTotal": "耗时 {duration}",
      "running": "运行中"
    }
  },
  "en": {
    "subagentActivity": {
      "noSession": "This sub-agent has no child session yet.",
      "liveEmpty": "No activity yet — waiting for the first event…",
      "offline": "Live connection lost — reconnecting automatically…",
      "turn": "Turn {id}",
      "turnTotal": "took {duration}",
      "running": "running"
    }
  },
  "ja": {
    "subagentActivity": {
      "noSession": "このサブエージェントにはまだ子セッションがありません。",
      "liveEmpty": "まだ実況イベントはありません（最初のイベントを待っています）…",
      "offline": "実況の接続が切れました。自動で再接続しています…",
      "turn": "ターン {id}",
      "turnTotal": "所要 {duration}",
      "running": "実行中"
    }
  },
  "ko": {
    "subagentActivity": {
      "noSession": "이 서브에이전트에는 아직 자식 세션이 없습니다.",
      "liveEmpty": "아직 실시간 이벤트가 없습니다 — 첫 이벤트를 기다리는 중…",
      "offline": "실시간 연결이 끊겼습니다 — 자동으로 다시 연결하는 중…",
      "turn": "턴 {id}",
      "turnTotal": "소요 {duration}",
      "running": "실행 중"
    }
  }
}
</i18n>
