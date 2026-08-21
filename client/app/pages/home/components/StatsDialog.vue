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
      "rangeMonth": "Last 30 Days"
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
      "rangeMonth": "近30日"
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
      "rangeMonth": "최근 30일"
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
      "rangeMonth": "近三十天"
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
      <!-- 范围切换 + 刷新 -->
      <div class="shrink-0 flex items-center justify-center gap-2">
        <SelectButton
          v-model="selectedRange"
          :options="rangeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false" />
        <Button
          icon="pi pi-refresh"
          text
          rounded
          :loading="loading"
          :aria-label="t('stats.refresh')"
          @click="loadStats" />
      </div>

      <!-- 图表渲染区 -->
      <div class="relative w-full overflow-hidden" style="height: 56vh;">
        <v-chart
          v-if="!loading && !empty && !error"
          class="w-full h-full"
          :option="chartOption"
          autoresize />

        <!-- 加载中 -->
        <div
          v-if="loading"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('stats.loading') }}</span>
        </div>

        <!-- 空态 -->
        <div
          v-else-if="empty"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-chart-bar text-6xl text-gray-300 dark:text-gray-600" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('stats.empty') }}
          </div>
        </div>

        <!-- 错误态 -->
        <div
          v-else-if="error"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-exclamation-triangle text-5xl text-red-400 dark:text-red-500" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('stats.loadError') }}
          </div>
          <Button
            :label="t('stats.refresh')"
            icon="pi pi-refresh"
            severity="secondary"
            @click="loadStats" />
        </div>
      </div>
    </div>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { use } from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import VChart from 'vue-echarts';
import { fetchApi } from '~/composables/requestApi';

use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emits('update:modelValue', v),
});

/** 后端单日单模型 token 用量（snake_case 原样保留） */
interface BackendModelUsage {
  model_name: string;
  input_tokens: number;
  output_tokens: number;
}

/** 后端单日用量 */
interface BackendDayUsage {
  date: string;
  by_model: BackendModelUsage[];
}

/** 后端统计响应（snake_case 原样保留） */
interface StatsResponse {
  range: string;
  days: BackendDayUsage[];
}

/** 映射后的单日用量（camelCase） */
interface DayUsage {
  date: string;
  byModel: BackendModelUsage[];
}

/** 范围选项 */
type RangeValue = 'day' | 'week' | 'month';

const rangeOptions = computed(() => [
  { label: t('stats.rangeDay'), value: 'day' as RangeValue },
  { label: t('stats.rangeWeek'), value: 'week' as RangeValue },
  { label: t('stats.rangeMonth'), value: 'month' as RangeValue }
]);

const selectedRange = ref<RangeValue>('week');
const loading = ref(false);
const empty = ref(false);
const error = ref(false);
const days = ref<DayUsage[]>([]);

const colorMode = useColorMode();

/** 当前是否为深色主题 */
const isDark = () => colorMode.value === 'dark';

/** 模型 → 颜色（浅色主题） */
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

/** 模型 → 颜色（深色主题） */
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

/** 按模型名取稳定颜色 */
const modelColor = (index: number): string => {
  const palette = isDark() ? DARK_PALETTE : LIGHT_PALETTE;
  return palette[index % palette.length];
};

/** 后端数据 → 前端映射 */
const mapStatsData = (payload: StatsResponse): DayUsage[] => {
  const rawDays = Array.isArray(payload?.days) ? payload.days : [];
  return rawDays.map(day => ({
    date: day.date,
    byModel: Array.isArray(day.by_model) ? day.by_model : []
  }));
};

/** 构建 echarts 堆叠柱状图配置 */
const chartOption = computed(() => {
  const dark = isDark();
  const axisLabelColor = dark ? '#9ca3af' : '#6b7280';
  const axisLineColor = dark ? '#3f4650' : '#d1d5db';
  const splitLineColor = dark ? '#2a2e35' : '#e5e7eb';
  const legendTextColor = dark ? '#e5e7eb' : '#1f2937';
  const tooltipBg = dark ? '#1a1d21' : '#ffffff';
  const tooltipBorder = dark ? '#3f4650' : '#d1d5db';
  const tooltipText = dark ? '#e5e7eb' : '#1f2937';

  // 收集所有出现的模型名（保持出现顺序）
  const modelNames: string[] = [];
  for (const day of days.value) {
    for (const usage of day.byModel) {
      if (!modelNames.includes(usage.model_name)) {
        modelNames.push(usage.model_name);
      }
    }
  }

  const xLabels = days.value.map(day => day.date);

  // 每个模型一个堆叠系列，值为该模型当日 input+output 之和
  const series = modelNames.map((modelName, index) => ({
    name: modelName,
    type: 'bar' as const,
    stack: 'total',
    emphasis: { focus: 'series' as const },
    itemStyle: { color: modelColor(index) },
    data: days.value.map(day => {
      const usage = day.byModel.find(u => u.model_name === modelName);
      return usage ? usage.input_tokens + usage.output_tokens : 0;
    })
  }));

  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
      backgroundColor: tooltipBg,
      borderColor: tooltipBorder,
      textStyle: { color: tooltipText }
    },
    legend: {
      data: modelNames,
      textStyle: { color: legendTextColor },
      top: 8
    },
    grid: {
      left: 16,
      right: 16,
      top: 48,
      bottom: 8,
      containLabel: true
    },
    xAxis: {
      type: 'category' as const,
      data: xLabels,
      axisLabel: { color: axisLabelColor },
      axisLine: { lineStyle: { color: axisLineColor } },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value' as const,
      name: 'token',
      nameTextStyle: { color: axisLabelColor },
      axisLabel: { color: axisLabelColor },
      splitLine: { lineStyle: { color: splitLineColor } }
    },
    series
  };
});

/** 拉取并渲染统计图表 */
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
    days.value = mapped;
  } catch (e) {
    console.error('[Stats] Failed to load token usage:', e);
    error.value = true;
    days.value = [];
  } finally {
    loading.value = false;
  }
};

/** 范围切换时重新拉取 */
watch(selectedRange, () => {
  loadStats();
});

/** 弹窗关闭后重置状态 */
const onHide = () => {
  loading.value = false;
  empty.value = false;
  error.value = false;
  days.value = [];
  selectedRange.value = 'week';
};
</script>
