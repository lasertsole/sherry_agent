<i18n lang="json">
{
  "en": {
    "stats": {
      "title": "Statistics",
      "loading": "Loading…",
      "empty": "No token usage data",
      "loadError": "Failed to load",
      "refresh": "Refresh",
      "rangeDay": "Today",
      "rangeWeek": "Last 7 Days",
      "rangeMonth": "Last 30 Days",
      "modeValue": "Absolute",
      "modePercent": "Share"
    }
  },
  "ja": {
    "stats": {
      "title": "統計",
      "loading": "読み込み中…",
      "empty": "トークン使用量データがありません",
      "loadError": "読み込みに失敗しました",
      "refresh": "更新",
      "rangeDay": "今日",
      "rangeWeek": "近7日",
      "rangeMonth": "近30日",
      "modeValue": "絶対値",
      "modePercent": "比率"
    }
  },
  "ko": {
    "stats": {
      "title": "통계",
      "loading": "불러오는 중…",
      "empty": "토큰 사용량 데이터가 없습니다",
      "loadError": "불러오기 실패",
      "refresh": "새로고침",
      "rangeDay": "오늘",
      "rangeWeek": "최근 7일",
      "rangeMonth": "최근 30일",
      "modeValue": "절대값",
      "modePercent": "비율"
    }
  },
  "zh": {
    "stats": {
      "title": "统计",
      "loading": "加载中…",
      "empty": "暂无 Token 用量数据",
      "loadError": "加载失败",
      "refresh": "刷新",
      "rangeDay": "今日",
      "rangeWeek": "近七天",
      "rangeMonth": "近三十天",
      "modeValue": "绝对值",
      "modePercent": "占比"
    }
  }
}
</i18n>

<template>
  <Dialog
    v-model:visible="visible"
    :header="t('stats.title')"
    :modal="true"
    :closable="true"
    class="w-[min(95vw,1200px)]"
    @show="loadStats"
    @hide="onHide">
    <div class="flex flex-col gap-3">
      <!-- Range switch + refresh -->
      <div class="shrink-0 flex items-center justify-center gap-2">
        <SelectButton
          v-model="selectedRange"
          :options="rangeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false" />
        <SelectButton
          v-model="chartMode"
          :options="modeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false" />
        <Button
          v-debounce:click.300="loadStats"
          icon="pi pi-refresh"
          text
          rounded
          :loading="loading"
          :aria-label="t('stats.refresh')" />
      </div>

      <!-- Chart rendering area -->
      <div
        class="relative w-full overflow-hidden"
        style="height: 56vh">
        <GChart
          v-if="!loading && !empty && !error"
          class="w-full h-full"
          :options="chartOption" />

        <!-- Loading -->
        <div
          v-if="loading"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('stats.loading') }}</span>
        </div>

        <!-- Empty state -->
        <div
          v-else-if="empty"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-chart-bar text-6xl text-gray-300 dark:text-gray-600" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('stats.empty') }}
          </div>
        </div>

        <!-- Error state -->
        <div
          v-else-if="error"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-exclamation-triangle text-5xl text-red-400 dark:text-red-500" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('stats.loadError') }}
          </div>
          <Button
            v-debounce:click.300="loadStats"
            :label="t('stats.refresh')"
            icon="pi pi-refresh"
            severity="secondary"
            :loading="loading" />
        </div>
      </div>
    </div>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { G2Spec } from '@antv/g2';
import GChart from './GChart.vue';
import { fetchApi } from '~/composables/requestApi';
import { vDebounce } from '~/directives/debounce';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

/** Per-day per-model token usage from the backend (snake_case kept as-is) */
interface BackendModelUsage {
  model_name: string;
  input_tokens: number;
  output_tokens: number;
}

/** Per-day usage from the backend */
interface BackendDayUsage {
  date: string;
  by_model: BackendModelUsage[];
}

/** Backend stats response (snake_case kept as-is) */
interface StatsResponse {
  range: string;
  days: BackendDayUsage[];
}

/** Mapped per-day usage (camelCase) */
interface DayUsage {
  date: string;
  byModel: BackendModelUsage[];
}

/** Range options */
type RangeValue = 'day' | 'week' | 'month';

const rangeOptions = computed(() => [
  { label: t('stats.rangeDay'), value: 'day' as RangeValue },
  { label: t('stats.rangeWeek'), value: 'week' as RangeValue },
  { label: t('stats.rangeMonth'), value: 'month' as RangeValue }
]);

/** Chart mode options: absolute values / share (100% stacked) */
type ChartModeValue = 'value' | 'percent';

const modeOptions = computed(() => [
  { label: t('stats.modeValue'), value: 'value' as ChartModeValue },
  { label: t('stats.modePercent'), value: 'percent' as ChartModeValue }
]);

const selectedRange = ref<RangeValue>('week');
const chartMode = ref<ChartModeValue>('value');
const loading = ref(false);
const empty = ref(false);
const error = ref(false);
const days = ref<DayUsage[]>([]);

const colorMode = useColorMode();

/** Whether the current theme is dark */
const isDark = () => colorMode.value === 'dark';

/** Model → color (light theme) */
const LIGHT_PALETTE: string[] = [
  '#3b82f6',
  '#f59e0b',
  '#10b981',
  '#ef4444',
  '#8b5cf6',
  '#06b6d4',
  '#ec4899',
  '#84cc16',
  '#f97316',
  '#14b8a6'
];

/** Model → color (dark theme) */
const DARK_PALETTE: string[] = [
  '#60a5fa',
  '#fbbf24',
  '#34d399',
  '#f87171',
  '#a78bfa',
  '#22d3ee',
  '#f472b6',
  '#a3e635',
  '#fb923c',
  '#2dd4bf'
];

/** Stable color by model name */
const modelColor = (index: number): string => {
  const palette = isDark() ? DARK_PALETTE : LIGHT_PALETTE;
  return palette[index % palette.length] ?? '';
};

/** Backend data → frontend mapping */
const mapStatsData = (payload: StatsResponse): DayUsage[] => {
  const rawDays = Array.isArray(payload?.days) ? payload.days : [];
  return rawDays.map(day => ({
    date: day.date,
    byModel: Array.isArray(day.by_model) ? day.by_model : []
  }));
};

/** Build the G2 stacked bar chart config */
const chartOption = computed<G2Spec>(() => {
  const dark = isDark();
  // Axis (tick labels/axis line/grid line) colors: white in dark theme, black in light theme, to guarantee contrast
  const axisColor = dark ? '#ffffff' : '#000000';
  // Bar stroke: keep the original low-contrast theme color, not following the axis color (avoids overly heavy white/black strokes)
  const barStrokeColor = dark ? '#3f4650' : '#d1d5db';
  const legendTextColor = dark ? '#e5e7eb' : '#1f2937';
  // Share mode: stackY + normalizeY → 100% stacked bars with percentage y-axis ticks
  const percent = chartMode.value === 'percent';

  // Collect all model names that appear (preserving order of appearance). If there is no model
  // data at all, days may be non-empty while modelNames is empty — an empty scale makes the G2
  // legend layout (computeCategoryLegendSize) read uninitialized items and crash, so we fall
  // back to an empty spec here.
  const modelNames: string[] = [];
  for (const day of days.value) {
    for (const usage of day.byModel) {
      if (!modelNames.includes(usage.model_name)) {
        modelNames.push(usage.model_name);
      }
    }
  }
  if (modelNames.length === 0) return { type: 'interval', data: [] } as G2Spec;

  // Long-form data: each row = sum of input+output tokens for one model on one day
  const data: { date: string; model: string; tokens: number }[] = [];
  for (const day of days.value) {
    for (const modelName of modelNames) {
      const usage = day.byModel.find(u => u.model_name === modelName);
      data.push({
        date: day.date,
        model: modelName,
        tokens: usage ? usage.input_tokens + usage.output_tokens : 0
      });
    }
  }

  return {
    type: 'interval',
    autoFit: true,
    data,
    encode: { x: 'date', y: 'tokens', color: 'model' },
    transform: percent ? [{ type: 'stackY' }, { type: 'normalizeY' }] : [{ type: 'stackY' }],
    scale: {
      color: {
        domain: modelNames,
        range: modelNames.map((_, index) => modelColor(index))
      }
    },
    theme: {
      type: dark ? 'classicDark' : 'classic',
      view: { viewFill: 'transparent' },
      axis: {
        labelFill: axisColor,
        lineStroke: axisColor,
        gridStroke: axisColor,
        titleFill: axisColor
      }
    },
    tooltip: {
      title: 'date',
      items: [
        // Hover block: shows "model name: token count (percentage of that day's total)"
        (
          datum: { date: string; model: string; tokens: number },
          _index: number | undefined,
          all: { date: string; model: string; tokens: number }[] | undefined
        ) => {
          const dayTotal = (all ?? []).reduce((sum, d) => (d.date === datum.date ? sum + d.tokens : sum), 0);
          const pct = dayTotal > 0 ? ((datum.tokens / dayTotal) * 100).toFixed(1) : '0.0';
          return {
            name: datum.model,
            value: `${datum.tokens.toLocaleString('en-US')} (${pct}%)`
          };
        }
      ]
    },
    axis: {
      x: {
        labelFill: axisColor,
        lineStroke: axisColor,
        line: true,
        lineStrokeOpacity: 1,
        lineLineWidth: 1,
        tick: false,
        titleFill: axisColor
      },
      y: {
        labelFill: axisColor,
        lineStroke: axisColor,
        line: true,
        lineStrokeOpacity: 1,
        lineLineWidth: 1,
        gridStroke: axisColor,
        gridStrokeOpacity: 1,
        gridLineWidth: 1,
        title: percent ? '%' : 'token',
        titleFill: axisColor,
        labelFormatter: percent ? (d: number) => `${Math.round(d * 100)}%` : undefined,
        grid: true
      }
    },
    style: {
      minHeight: 6,
      radiusTopLeft: 4,
      radiusTopRight: 4,
      stroke: barStrokeColor,
      lineWidth: 1,
      // With a single date (today) the bar fills the whole x band; cap the max width to avoid overly wide bars;
      // the week/month multi-date cases are not capped, keeping the original look
      maxWidth: days.value.length <= 2 ? 64 : undefined
    },
    legend: {
      color: { position: 'top', itemLabelFill: legendTextColor, itemSpacing: [8, 4, 4] }
    }
  };
});

/** Fetch and render the statistics chart */
const loadStats = async () => {
  loading.value = true;
  error.value = false;
  empty.value = false;
  try {
    const payload = (await fetchApi({
      url: '/stats/tokens',
      opts: { range: selectedRange.value },
      method: 'get'
    })) as unknown;
    const response = payload as StatsResponse;
    const mapped = mapStatsData(response);
    if (mapped.length === 0) {
      empty.value = true;
      days.value = [];
      return;
    }
    // No model usage at all (days non-empty but every by_model empty) → treat as empty state, avoiding
    // an empty scale triggering the G2 legend layout crash (computeCategoryLegendSize reading undefined).
    const hasModelData = mapped.some(day => day.byModel.length > 0);
    if (!hasModelData) {
      empty.value = true;
      days.value = [];
      return;
    }
    days.value = mapped;
  } catch (e) {
    console.error('[Stats] Failed to load token usage:', e);
    error.value = true;
    days.value = [];
  } finally {
    loading.value = false;
  }
};

/** Re-fetch when the range changes */
watch(selectedRange, () => {
  loadStats();
});

/** Reset state after the dialog closes */
const onHide = () => {
  loading.value = false;
  empty.value = false;
  error.value = false;
  days.value = [];
  selectedRange.value = 'week';
  chartMode.value = 'value';
};
</script>
