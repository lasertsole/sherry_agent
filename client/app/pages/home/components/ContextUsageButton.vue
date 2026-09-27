<template>
  <!-- Ring entry: the arc is the share of the context window in use; clicking opens
       the breakdown above the toolbar. -->
  <ToolbarPopover>
    <template #trigger="{ toggle }">
      <button
        type="button"
        class="flex cursor-pointer items-center justify-center rounded-full p-1 transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
        :title="summary"
        :aria-label="t('contextUsage.label')"
        @click="togglePopover(toggle)">
        <svg
          class="h-5 w-5"
          viewBox="0 0 24 24"
          :aria-hidden="true">
          <!-- The arcs start at the top (hence the -90° on each circle); the tick
             below is NOT rotated with them, so its angle is the ring's own scale
             (fraction × 360°, clockwise from the top). -->
          <circle
            cx="12"
            cy="12"
            r="9"
            fill="none"
            stroke-width="2.5"
            transform="rotate(-90 12 12)"
            class="stroke-gray-200 dark:stroke-gray-700" />
          <circle
            cx="12"
            cy="12"
            r="9"
            fill="none"
            stroke-width="2.5"
            stroke-linecap="round"
            transform="rotate(-90 12 12)"
            :class="ringClass"
            :stroke-dasharray="RING_LENGTH"
            :stroke-dashoffset="ringOffset" />
          <!-- Compression threshold tick: same angle scale as the dash (0% at the
             top after the svg's -90° rotation). -->
          <line
            v-if="compressPercent > 0"
            x1="12"
            y1="2"
            x2="12"
            y2="5.4"
            stroke-width="2"
            class="stroke-red-500"
            :transform="`rotate(${compressPercent * 3.6} 12 12)`"
            :aria-hidden="true" />
        </svg>
      </button>
    </template>

    <div class="flex w-72 flex-col gap-3">
      <!-- Window header: the track is the whole context window; the filled part is
             this session's last prompt, split into the three coloured parts of the
             legend below. -->
      <div class="flex flex-col gap-1">
        <div class="flex items-baseline justify-between gap-3">
          <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
            {{ t('contextUsage.capacity') }}
          </span>
          <span
            v-if="compressPercent > 0"
            class="text-xs font-medium text-red-500">
            {{ t('contextUsage.compressAt', { percent: compressPercent.toFixed(0) }) }}
          </span>
        </div>
        <div class="relative flex h-2 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
          <!-- One segment per part: its share of the prompt, sized against the
               window. Widths use the same basis as the parts' percentages. -->
          <div
            v-for="part in parts"
            :key="part.key"
            data-test="stack-segment"
            class="h-full transition-all"
            :class="part.color"
            :style="{ width: `${segmentWidth(part.tokens)}%` }"
            :title="`${t(`contextUsage.${part.key}`)} · ${shareLabel(part.tokens)}`"></div>
          <!-- Compression threshold: the pressure at which summarization compacts. -->
          <div
            class="absolute inset-y-0 border-l border-dashed border-red-500"
            :style="{ left: `${compressPercent}%` }"
            :title="t('contextUsage.compressAt', { percent: compressPercent.toFixed(0) })"
            :aria-label="t('contextUsage.compressAt', { percent: compressPercent.toFixed(0) })"
            role="separator"></div>
        </div>
        <div class="flex items-baseline justify-between text-xs text-gray-500 dark:text-gray-400">
          <span>{{ t('contextUsage.of', { used: wan(total), window: wan(window) }) }}</span>
          <span class="font-medium text-gray-700 dark:text-gray-200">{{ percentLabel }}</span>
        </div>
      </div>

      <!-- Legend: the colours that divide the bar above (no bars of their own). -->
      <ul class="flex flex-col gap-1.5">
        <li
          v-for="part in parts"
          :key="part.key"
          data-test="legend-row"
          class="flex items-baseline justify-between gap-2 text-xs">
          <span class="flex min-w-0 items-center gap-1.5 text-gray-600 dark:text-gray-300">
            <span
              class="h-2 w-2 shrink-0 rounded-full"
              :class="part.color"></span>
            <span class="truncate">{{ t(`contextUsage.${part.key}`) }}</span>
          </span>
          <span class="shrink-0 text-gray-500 dark:text-gray-400">
            {{ shareLabel(part.tokens) }}
          </span>
        </li>
      </ul>

      <!-- Session-wide cache hit rate: how much of the prompt the provider
             served from its prompt cache (— when it never reported any). -->
      <div
        class="flex items-baseline justify-between border-t border-solid border-gray-100 pt-2 text-xs dark:border-gray-800">
        <span class="text-gray-600 dark:text-gray-300">{{ t('contextUsage.cacheHit') }}</span>
        <span class="font-medium text-gray-700 dark:text-gray-200">{{ cacheHitLabel }}</span>
      </div>
    </div>
  </ToolbarPopover>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import ToolbarPopover from './ToolbarPopover.vue';

const props = defineProps<{
  sessionId: string;
  /** Prompt size of the session's newest finished turn (0 before the first answer). */
  usedTokens: number;
}>();

const { t } = useI18n({ useScope: 'local' });

const store = useContextUsageStore();

/** Ring geometry: r = 9 in a 24-box, so the dash length is the circumference. */
const RING_LENGTH = 2 * Math.PI * 9;
/** Share of the window at which the ring turns amber, then red. */
const WARN_RATIO = 0.75;
const DANGER_RATIO = 0.9;

/** Accounting the backend reported (null until the first read). */
const usage = computed(() => store.usageFor(props.sessionId));
/** Window size: the reported one, else unknown (0 → the ring stays empty). */
const window = computed(() => usage.value?.window ?? 0);
/** Tokens in use: fresh numbers when we have them, else the caller's value. */
const total = computed(() => usage.value?.total || props.usedTokens);
/** Fill ratio of the window, clamped to a full ring. */
const percent = computed(() => {
  if (!window.value || window.value <= 0) return 0;
  return Math.min(Math.max(total.value / window.value, 0), 1);
});

/** Ring stroke colour: calm, amber, then red as the window fills up. */
const ringClass = computed(() =>
  percent.value >= DANGER_RATIO
    ? 'stroke-red-500'
    : percent.value >= WARN_RATIO
      ? 'stroke-amber-500'
      : 'stroke-theme-main'
);
const ringOffset = computed(() => RING_LENGTH * (1 - percent.value));

/** The three parts of the prompt, in the order the panel lists them. */
const parts = computed(() => [
  { key: 'messages', tokens: usage.value?.messages ?? 0, color: 'bg-sky-500' },
  { key: 'system', tokens: usage.value?.system ?? 0, color: 'bg-violet-500' },
  { key: 'tools', tokens: usage.value?.tools ?? 0, color: 'bg-emerald-500' }
]);

/**
 * Width of a part's segment in the stacked bar: its share of the context WINDOW,
 * so the segments add up to the prompt's own fill and the rest stays the track.
 * @param tokens
 */
const segmentWidth = (tokens: number): number => {
  if (!window.value || window.value <= 0) return 0;
  return Math.min((tokens / window.value) * 100, 100);
};

/**
 * Share of a part in the reported prompt.
 * @param tokens
 */
const share = (tokens: number): number => {
  if (!total.value || total.value <= 0) return 0;
  return Math.min(tokens / total.value, 1);
};

/**
 * Percentage label of a share — one decimal, like the header's own figure.
 * @param tokens
 */
const shareLabel = (tokens: number): string => `${(share(tokens) * 100).toFixed(1)}%`;

const percentLabel = computed(() => `${(percent.value * 100).toFixed(1)}%`);

/** Where the red dashed compression threshold sits on the bar (share of window). */
const compressPercent = computed(() => {
  const ratio = usage.value?.compress_ratio ?? 0;
  return Math.min(Math.max(ratio * 100, 0), 100);
});

/** Session-wide cache hit rate, or a dash when the provider never reported one. */
const cacheHitLabel = computed(() => {
  const ratio = usage.value?.cache_hit_ratio;
  return typeof ratio === 'number' ? `${(ratio * 100).toFixed(1)}%` : '—';
});

/**
 * Token count in 万 (10k), the unit the panel reads in.
 * @param tokens
 */
const wan = (tokens: number): string => {
  const value = tokens / 10_000;
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)}万`;
};

/** Tooltip/aria summary: usage, window and share in one line. */
const summary = computed(() =>
  t('contextUsage.summary', { used: wan(total.value), window: wan(window.value), percent: percentLabel.value })
);

/**
 * Open the breakdown panel (and refresh it: a turn may have just landed).
 * @param toggle The popover's own toggle (bound through the trigger slot).
 */
const togglePopover = (toggle: () => void) => {
  void store.refresh(props.sessionId);
  toggle();
};

onMounted(() => void store.refresh(props.sessionId));
// A finished turn reports a new prompt size: refresh so the ring and the
// breakdown describe the turn that just ran.
watch(
  () => props.usedTokens,
  () => void store.refresh(props.sessionId)
);
// Session switch: the numbers belong to the session being shown.
watch(
  () => props.sessionId,
  sessionId => {
    if (sessionId) void store.refresh(sessionId);
  }
);
</script>

<i18n lang="json">
{
  "zh": {
    "contextUsage": {
      "label": "上下文占用",
      "capacity": "上下文容量",
      "of": "{used} / {window}",
      "summary": "上下文 {used} / {window}（{percent}）",
      "messages": "消息",
      "system": "系统提示词",
      "tools": "工具调用",
      "compressAt": "压缩阈值 {percent}%",
      "cacheHit": "平均缓存命中率"
    }
  },
  "en": {
    "contextUsage": {
      "label": "Context usage",
      "capacity": "Context capacity",
      "of": "{used} / {window}",
      "summary": "Context {used} / {window} ({percent})",
      "messages": "Messages",
      "system": "System prompt",
      "tools": "Tool calls",
      "compressAt": "Compaction threshold {percent}%",
      "cacheHit": "Average cache hit rate"
    }
  },
  "ja": {
    "contextUsage": {
      "label": "コンテキスト使用量",
      "capacity": "コンテキスト容量",
      "of": "{used} / {window}",
      "summary": "コンテキスト {used} / {window}（{percent}）",
      "messages": "メッセージ",
      "system": "システムプロンプト",
      "tools": "ツール呼び出し",
      "compressAt": "圧縮しきい値 {percent}%",
      "cacheHit": "平均キャッシュヒット率"
    }
  },
  "ko": {
    "contextUsage": {
      "label": "컨텍스트 사용량",
      "capacity": "컨텍스트 용량",
      "of": "{used} / {window}",
      "summary": "컨텍스트 {used} / {window}({percent})",
      "messages": "메시지",
      "system": "시스템 프롬프트",
      "tools": "도구 호출",
      "cacheHit": "평균 캐시 적중률",
      "compressAt": "압축 임계값 {percent}%"
    }
  }
}
</i18n>
