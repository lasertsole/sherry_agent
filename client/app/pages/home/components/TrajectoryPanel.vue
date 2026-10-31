<template>
  <div class="flex h-full min-h-0 flex-col gap-2 p-4">
    <div class="flex flex-wrap items-center gap-1">
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        :loading="loading"
        :title="t('trajectoryPanel.refresh')"
        :aria-label="t('trajectoryPanel.refresh')"
        @click="load" />
      <Button
        v-if="events.length"
        icon="pi pi-search"
        size="small"
        text
        severity="secondary"
        :title="t('trajectoryPanel.search')"
        :aria-label="t('trajectoryPanel.search')"
        @click="toggleSearch" />
      <div class="relative">
        <Button
          v-if="events.length"
          icon="pi pi-filter"
          size="small"
          text
          severity="secondary"
          :title="t('trajectoryPanel.expandAll')"
          :aria-label="t('trajectoryPanel.expandAll')"
          @click="foldMenuOpen = !foldMenuOpen" />
        <div
          v-if="foldMenuOpen"
          class="absolute left-0 top-full z-30 mt-1 w-56 rounded-lg border border-solid border-gray-200 bg-white p-3 shadow-lg dark:border-gray-700 dark:bg-gray-900">
          <label class="flex items-center justify-between gap-2 text-xs text-gray-600 dark:text-gray-300">
            <span>{{ t('trajectoryPanel.toolsSection') }}</span>
            <ToggleSwitch v-model="toolsCollapsed" />
          </label>
          <label class="mt-2 flex items-center justify-between gap-2 text-xs text-gray-600 dark:text-gray-300">
            <span>{{ t('trajectoryPanel.lifecycleSection') }}</span>
            <ToggleSwitch v-model="lifecycleCollapsed" />
          </label>
          <div class="mt-3 flex gap-2">
            <Button
              :label="t('trajectoryPanel.expandAll')"
              size="small"
              text
              @click="expandAll" />
            <Button
              :label="t('trajectoryPanel.collapseAll')"
              size="small"
              text
              @click="collapseAll" />
          </div>
        </div>
      </div>
      <Button
        v-if="turns.length"
        :label="turnFilterLabel"
        icon="pi pi-list"
        size="small"
        text
        severity="secondary"
        :title="t('trajectoryPanel.turnFilter')"
        :aria-label="t('trajectoryPanel.turnFilter')"
        @click="openTurnMenu" />
      <Menu
        ref="turnMenu"
        :model="turnMenuItems"
        popup />
    </div>

    <p
      v-if="events.length"
      class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{
        t('trajectoryPanel.summary', {
          events: summary.eventCount,
          tools: summary.toolCount,
          duration: formatDuration(summary.totalToolMs)
        })
      }}
    </p>

    <div
      v-if="searchOpen"
      class="flex items-center gap-1 rounded-lg border border-solid border-gray-200 px-2 py-1 dark:border-gray-700">
      <input
        ref="searchInput"
        v-model="query"
        type="search"
        class="min-w-0 flex-1 bg-transparent text-xs outline-none"
        :placeholder="t('trajectoryPanel.searchPlaceholder')"
        @keydown.enter.exact.prevent="nextMatch"
        @keydown.enter.shift.prevent="previousMatch"
        @keydown.esc.prevent="closeSearch" />
      <span class="shrink-0 text-xs text-gray-400">{{ matchLabel }}</span>
      <Button
        icon="pi pi-chevron-up"
        size="small"
        text
        severity="secondary"
        :aria-label="t('trajectoryPanel.searchPrevious')"
        @click="previousMatch" />
      <Button
        icon="pi pi-chevron-down"
        size="small"
        text
        severity="secondary"
        :aria-label="t('trajectoryPanel.searchNext')"
        @click="nextMatch" />
      <Button
        icon="pi pi-times"
        size="small"
        text
        severity="secondary"
        :aria-label="t('trajectoryPanel.close')"
        @click="closeSearch" />
    </div>

    <div
      v-if="loading"
      class="flex flex-1 items-center justify-center py-8">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>
    <p
      v-else-if="!sid"
      class="m-0 py-8 text-center text-sm text-gray-400">
      {{ t('trajectoryPanel.noSession') }}
    </p>
    <p
      v-else-if="error"
      class="m-0 py-8 text-center text-sm text-red-500 dark:text-red-400">
      {{ error }}
    </p>
    <p
      v-else-if="!events.length"
      class="m-0 py-8 text-center text-sm text-gray-400">
      {{ t('trajectoryPanel.empty') }}
    </p>

    <div
      v-else
      ref="scroller"
      class="min-h-0 flex-1 overflow-y-auto pr-1">
      <section
        v-for="turn in visibleTurns"
        :key="turn.turnId"
        :data-turn-id="turn.turnId"
        class="mb-3">
        <header
          class="sticky top-0 z-10 mb-1 flex flex-wrap items-center gap-x-3 gap-y-1 rounded bg-gray-50 px-2 py-1 text-xs text-gray-500 dark:bg-gray-900/90 dark:text-gray-400">
          <span class="font-mono text-gray-700 dark:text-gray-200">{{ shortTurn(turn.turnId) }}</span>
          <span>{{ formatClock(turn.events[0]!.ts) }}</span>
          <span v-if="turnEndOf(turn)">
            {{
              t('trajectoryPanel.turnEnd', {
                total: formatDuration(Number(turnEndOf(turn)!.payload.total_ms ?? 0)),
                ttfb:
                  turnEndOf(turn)!.payload.ttfb_ms == null
                    ? '—'
                    : formatDuration(Number(turnEndOf(turn)!.payload.ttfb_ms))
              })
            }}
          </span>
        </header>

        <div
          v-for="event in turn.events"
          :key="event.event_id"
          :data-event-id="event.event_id"
          class="mb-1 rounded border border-solid border-gray-100 dark:border-gray-800">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs hover:bg-gray-50 dark:hover:bg-gray-900/60"
            @click="toggleRow(event)">
            <span
              v-if="event.kind === 'tool_start'"
              :class="event.is_error ? 'pi pi-exclamation-triangle text-red-500' : 'pi pi-check text-green-500'"
              class="shrink-0" />
            <span
              v-else
              :class="kindClass(event.kind)"
              class="shrink-0 rounded px-1 py-0.5 text-[10px] font-medium uppercase">
              {{ event.kind }}
            </span>
            <span class="shrink-0 font-medium text-gray-700 dark:text-gray-200">
              {{ event.kind === 'tool_start' ? String(event.payload.tool_name ?? event.summary) : event.kind }}
            </span>
            <span class="min-w-0 flex-1 truncate font-mono text-gray-400">{{ previewOf(event) }}</span>
            <span
              v-if="event.kind === 'tool_start' && event.payload.duration_ms != null"
              class="shrink-0 tabular-nums text-gray-400">
              {{ formatDuration(Number(event.payload.duration_ms)) }}
            </span>
            <span class="shrink-0 text-gray-400">{{ formatClock(event.ts) }}</span>
            <span
              :class="[
                'pi pi-chevron-down shrink-0 text-[10px] transition-transform',
                { 'rotate-180': isExpanded(event) }
              ]" />
          </button>

          <div
            v-if="isExpanded(event)"
            class="flex flex-col gap-2 border-t border-solid border-gray-100 px-2 py-2 dark:border-gray-800">
            <template v-if="event.kind === 'tool_start'">
              <div v-if="hasArgs(event)">
                <div class="mb-1 flex items-center justify-between text-xs font-semibold text-gray-500">
                  <span>{{ t('trajectoryPanel.args') }}</span>
                  <button
                    type="button"
                    :class="copiedId === event.event_id ? 'pi pi-check' : 'pi pi-copy'"
                    class="text-gray-400"
                    :aria-label="t('trajectoryPanel.copy')"
                    @click.stop="copy(prettyJson(event.payload.args), event.event_id)" />
                </div>
                <pre
                  class="max-h-64 overflow-auto rounded border border-solid border-gray-200 bg-white p-2 text-xs whitespace-pre-wrap break-words dark:border-gray-700 dark:bg-gray-900/60"
                  >{{ prettyJson(event.payload.args) }}</pre>
              </div>
              <div v-if="event.payload.result !== undefined && event.payload.result !== null">
                <div class="mb-1 flex items-center justify-between text-xs font-semibold text-gray-500">
                  <span>{{ t('trajectoryPanel.result') }}</span>
                  <button
                    type="button"
                    :class="copiedId === event.event_id ? 'pi pi-check' : 'pi pi-copy'"
                    class="text-gray-400"
                    :aria-label="t('trajectoryPanel.copy')"
                    @click.stop="copy(String(event.payload.result), event.event_id)" />
                </div>
                <pre
                  class="max-h-64 overflow-auto rounded border border-solid border-gray-200 bg-white p-2 text-xs whitespace-pre-wrap break-words dark:border-gray-700 dark:bg-gray-900/60"
                  >{{ event.payload.result }}</pre>
              </div>
            </template>
            <template v-else>
              <div class="mb-1 flex items-center justify-between text-xs font-semibold text-gray-500">
                <span>{{ t('trajectoryPanel.payload') }}</span>
                <button
                  type="button"
                  :class="copiedId === event.event_id ? 'pi pi-check' : 'pi pi-copy'"
                  class="text-gray-400"
                  :aria-label="t('trajectoryPanel.copy')"
                  @click.stop="copy(prettyJson(event.payload), event.event_id)" />
              </div>
              <pre
                class="max-h-64 overflow-auto rounded border border-solid border-gray-200 bg-white p-2 text-xs whitespace-pre-wrap break-words dark:border-gray-700 dark:bg-gray-900/60"
                >{{ prettyJson(event.payload) }}</pre>
            </template>
          </div>
        </div>
      </section>

      <p
        v-if="truncated"
        class="m-0 py-2 text-center text-xs text-amber-600 dark:text-amber-400">
        {{ t('trajectoryPanel.truncated') }}
      </p>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import type { TrajectoryEvent } from '~/composables/bridge/session';
// The nested bridge module is not in the Nuxt auto-import set.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchTrajectory } from '~/composables/bridge/session';
import type { TrajectoryTurn } from '~/composables/use-trajectory';

const { t } = useI18n({ useScope: 'local' });
const route = useRoute();

const sid = computed(() => String(route.params.sid ?? ''));
const events = ref<TrajectoryEvent[]>([]);
const loading = ref(false);
const error = ref('');
const truncated = ref(false);

const query = ref('');
const searchOpen = ref(false);
const matchIndex = ref(0);
const toolsCollapsed = ref(true);
const lifecycleCollapsed = ref(false);
const expandedRows = ref<Set<string>>(new Set());
const turnFilter = ref('');
const foldMenuOpen = ref(false);
const turnMenu = ref<{ toggle: (event: Event) => void } | null>(null);
const scroller = ref<HTMLElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const copiedId = ref('');
let copiedTimer: ReturnType<typeof setTimeout> | undefined;

const turns = computed(() => groupTurns(events.value));
const summary = computed(() => summarizeTurns(turns.value));
const turnFilterLabel = computed(() =>
  turnFilter.value ? shortTurn(turnFilter.value) : t('trajectoryPanel.turnFilterAll')
);

const turnMenuItems = computed(() => [
  {
    label: t('trajectoryPanel.turnFilterAll'),
    icon: turnFilter.value ? '' : 'pi pi-check',
    command: () => (turnFilter.value = '')
  },
  ...turns.value.map(turn => ({
    label: `${shortTurn(turn.turnId)} · ${formatClock(turn.events[0]!.ts)}`,
    icon: turnFilter.value === turn.turnId ? 'pi pi-check' : '',
    command: () => (turnFilter.value = turn.turnId)
  }))
]);

const visibleTurns = computed<TrajectoryTurn[]>(() => {
  let list = turns.value;
  if (turnFilter.value) list = list.filter(turn => turn.turnId === turnFilter.value);
  const needle = query.value.trim();
  if (!needle) return list;
  return list
    .map(turn => ({ ...turn, events: turn.events.filter(event => eventMatches(event, needle)) }))
    .filter(turn => turn.events.length > 0);
});

const matchedEvents = computed(() => (query.value.trim() ? visibleTurns.value.flatMap(turn => turn.events) : []));
const matchLabel = computed(() =>
  t('trajectoryPanel.matchCount', {
    current: matchedEvents.value.length ? Math.min(matchIndex.value + 1, matchedEvents.value.length) : 0,
    total: matchedEvents.value.length
  })
);

const shortTurn = (turnId: string): string => turnId.slice(0, 8);

const turnEndOf = (turn: TrajectoryTurn): TrajectoryEvent | undefined =>
  turn.events.find(event => event.kind === 'turn_end');

/**
 * Whether a tool row carries a non-empty argument object.
 * @param event
 */
const hasArgs = (event: TrajectoryEvent): boolean => {
  const args = event.payload.args;
  return !!args && typeof args === 'object' && Object.keys(args).length > 0;
};

/**
 * Whether a row is open. A search hit is always open so the answer is visible;
 * otherwise the row's section default plus a per-row override applies.
 * @param event
 */
const isExpanded = (event: TrajectoryEvent): boolean => {
  if (query.value.trim() && eventMatches(event, query.value)) return true;
  if (expandedRows.value.has(event.event_id)) return true;
  return event.kind === 'tool_start' ? !toolsCollapsed.value : !lifecycleCollapsed.value;
};

const toggleRow = (event: TrajectoryEvent) => {
  const next = new Set(expandedRows.value);
  if (next.has(event.event_id)) next.delete(event.event_id);
  else next.add(event.event_id);
  expandedRows.value = next;
};

const expandAll = () => {
  toolsCollapsed.value = false;
  lifecycleCollapsed.value = false;
  expandedRows.value = new Set();
  foldMenuOpen.value = false;
};

const collapseAll = () => {
  toolsCollapsed.value = true;
  lifecycleCollapsed.value = true;
  expandedRows.value = new Set();
  foldMenuOpen.value = false;
};

const toggleSearch = () => {
  searchOpen.value = !searchOpen.value;
  if (!searchOpen.value) query.value = '';
  else nextTick(() => searchInput.value?.focus());
};

const closeSearch = () => {
  searchOpen.value = false;
  query.value = '';
};

const scrollToEvent = async (event: TrajectoryEvent) => {
  await nextTick();
  const row = scroller.value?.querySelector(`[data-event-id="${event.event_id}"]`);
  row?.scrollIntoView?.({ block: 'center' });
};

const nextMatch = () => {
  if (!matchedEvents.value.length) return;
  matchIndex.value = (matchIndex.value + 1) % matchedEvents.value.length;
  void scrollToEvent(matchedEvents.value[matchIndex.value]!);
};

const previousMatch = () => {
  if (!matchedEvents.value.length) return;
  matchIndex.value = (matchIndex.value - 1 + matchedEvents.value.length) % matchedEvents.value.length;
  void scrollToEvent(matchedEvents.value[matchIndex.value]!);
};

const openTurnMenu = (event: Event) => {
  turnMenu.value?.toggle(event);
};

const copy = async (text: string, id: string) => {
  try {
    await navigator.clipboard.writeText(text);
    copiedId.value = id;
    if (copiedTimer) clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => (copiedId.value = ''), 1200);
  } catch {
    // Clipboard permission is optional; the row stays readable.
  }
};

const kindClass = (kind: string): string => {
  if (kind === 'compact') return 'bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300';
  if (kind === 'model_retry') return 'bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300';
  if (kind === 'invalid_tool_call') return 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300';
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';
};

const scrollToBottom = async () => {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
};

const load = async () => {
  const sessionId = sid.value;
  if (!sessionId) {
    events.value = [];
    return;
  }
  loading.value = true;
  error.value = '';
  truncated.value = false;
  try {
    const first = await fetchTrajectory(sessionId, { limit: 500 });
    if (first === null) {
      error.value = t('trajectoryPanel.error');
      events.value = [];
      return;
    }
    let all = first;
    if (first.length >= 500) {
      const second = await fetchTrajectory(sessionId, { limit: 500, offset: 500 });
      if (second === null) {
        error.value = t('trajectoryPanel.error');
        events.value = [];
        return;
      }
      all = [...first, ...second];
      truncated.value = second.length >= 500;
    }
    events.value = all;
    matchIndex.value = 0;
    await scrollToBottom();
  } catch {
    error.value = t('trajectoryPanel.error');
    events.value = [];
  } finally {
    loading.value = false;
  }
};

watch(query, () => {
  matchIndex.value = 0;
  const first = matchedEvents.value[0];
  if (query.value.trim() && first) void nextTick(() => scrollToEvent(first));
});

watch(sid, () => void load(), { immediate: true });

onBeforeUnmount(() => {
  if (copiedTimer) clearTimeout(copiedTimer);
});
</script>

<i18n lang="json">
{
  "zh": {
    "trajectoryPanel": {
      "refresh": "刷新",
      "search": "搜索",
      "searchPlaceholder": "搜索工具、参数或事件",
      "searchNext": "下一个匹配",
      "searchPrevious": "上一个匹配",
      "matchCount": "{current}/{total}",
      "close": "关闭搜索",
      "expandAll": "全部展开",
      "collapseAll": "全部折叠",
      "toolsSection": "工具行默认折叠",
      "lifecycleSection": "生命周期行默认折叠",
      "turnFilter": "按回合筛选",
      "turnFilterAll": "全部回合",
      "turnEnd": "总时长 {total} · TTFT {ttfb}",
      "summary": "{events} 个事件 · {tools} 次工具调用 · 工具总时长 {duration}",
      "empty": "该会话还没有轨迹记录",
      "noSession": "请先打开一个会话",
      "error": "读取轨迹失败",
      "truncated": "仅显示最近 1000 条事件",
      "args": "参数",
      "result": "结果",
      "payload": "事件数据",
      "copy": "复制"
    }
  },
  "en": {
    "trajectoryPanel": {
      "refresh": "Refresh",
      "search": "Search",
      "searchPlaceholder": "Search tools, arguments or events",
      "searchNext": "Next match",
      "searchPrevious": "Previous match",
      "matchCount": "{current}/{total}",
      "close": "Close search",
      "expandAll": "Expand all",
      "collapseAll": "Collapse all",
      "toolsSection": "Collapse tool rows by default",
      "lifecycleSection": "Collapse lifecycle rows by default",
      "turnFilter": "Filter by turn",
      "turnFilterAll": "All turns",
      "turnEnd": "total {total} · TTFT {ttfb}",
      "summary": "{events} events · {tools} tool calls · {duration} tool time",
      "empty": "This session has no trajectory records yet",
      "noSession": "Open a session first",
      "error": "Failed to read the trajectory",
      "truncated": "Showing the most recent 1000 events",
      "args": "Arguments",
      "result": "Result",
      "payload": "Event data",
      "copy": "Copy"
    }
  },
  "ja": {
    "trajectoryPanel": {
      "refresh": "更新",
      "search": "検索",
      "searchPlaceholder": "ツール・引数・イベントを検索",
      "searchNext": "次の一致",
      "searchPrevious": "前の一致",
      "matchCount": "{current}/{total}",
      "close": "検索を閉じる",
      "expandAll": "すべて展開",
      "collapseAll": "すべて折りたたむ",
      "toolsSection": "ツール行を既定で折りたたむ",
      "lifecycleSection": "ライフサイクル行を既定で折りたたむ",
      "turnFilter": "ターンで絞り込む",
      "turnFilterAll": "すべてのターン",
      "turnEnd": "合計 {total} · TTFT {ttfb}",
      "summary": "{events} イベント · {tools} 回のツール呼び出し · ツール時間 {duration}",
      "empty": "このセッションにはまだ軌跡記録がありません",
      "noSession": "先にセッションを開いてください",
      "error": "軌跡の読み取りに失敗しました",
      "truncated": "最新 1000 件のイベントのみ表示",
      "args": "引数",
      "result": "結果",
      "payload": "イベントデータ",
      "copy": "コピー"
    }
  },
  "ko": {
    "trajectoryPanel": {
      "refresh": "새로고침",
      "search": "검색",
      "searchPlaceholder": "도구, 인수 또는 이벤트 검색",
      "searchNext": "다음 일치",
      "searchPrevious": "이전 일치",
      "matchCount": "{current}/{total}",
      "close": "검색 닫기",
      "expandAll": "모두 펼치기",
      "collapseAll": "모두 접기",
      "toolsSection": "도구 행 기본 접기",
      "lifecycleSection": "라이프사이클 행 기본 접기",
      "turnFilter": "턴으로 필터",
      "turnFilterAll": "모든 턴",
      "turnEnd": "총 {total} · TTFT {ttfb}",
      "summary": "{events}개 이벤트 · {tools}회 도구 호출 · 도구 시간 {duration}",
      "empty": "이 세션에는 아직 궤적 기록이 없습니다",
      "noSession": "먼저 세션을 여세요",
      "error": "궤적을 읽지 못했습니다",
      "truncated": "최근 1000개 이벤트만 표시",
      "args": "인수",
      "result": "결과",
      "payload": "이벤트 데이터",
      "copy": "복사"
    }
  }
}
</i18n>
