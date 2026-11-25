<template>
  <!-- Post-task session run report: token / context hit-rate / code-intel usage.
       Self-contained: fetch on mount, collapsible, no sidebar/store changes. -->
  <section
    class="mt-3 rounded border border-solid border-gray-100 dark:border-gray-800"
    data-test="session-report">
    <button
      type="button"
      class="flex w-full cursor-pointer select-none items-center gap-2 px-3 py-2 text-left text-xs"
      data-test="session-report-toggle"
      @click="expanded = !expanded">
      <i class="pi pi-chart-bar text-theme-main"></i>
      <span class="text-gray-600 dark:text-gray-300">{{ t('sessionReport.title') }}</span>
      <i
        class="pi pi-chevron-down ml-auto text-[10px] text-gray-400 transition-transform"
        :class="{ 'rotate-180': expanded }"></i>
    </button>

    <div
      v-if="expanded"
      class="border-t border-solid border-gray-100 px-3 py-2 text-xs dark:border-gray-800">
      <p
        v-if="loading"
        class="m-0 text-gray-400">
        {{ t('sessionReport.loading') }}
      </p>
      <p
        v-else-if="!report || report.error"
        class="m-0 text-gray-400">
        {{ t('sessionReport.empty') }}
      </p>
      <template v-else>
        <!-- Tokens -->
        <div class="mb-2">
          <div class="mb-1 font-semibold text-gray-700 dark:text-gray-200">
            {{ t('sessionReport.tokens') }}
          </div>
          <div class="font-mono text-gray-500 dark:text-gray-400">
            {{ t('sessionReport.grandTotal', { n: formatNum(report.tokens.grand_total) }) }}
            <span v-if="report.tokens.estimated_cost_usd !== undefined">
              · ~${{ report.tokens.estimated_cost_usd }}
            </span>
          </div>
          <div class="mt-0.5 text-gray-500 dark:text-gray-400">
            {{
              t('sessionReport.main', {
                i: formatNum(report.tokens.main.input),
                o: formatNum(report.tokens.main.output)
              })
            }}
          </div>
          <div
            v-for="child in report.tokens.children"
            :key="child.role"
            class="text-gray-500 dark:text-gray-400">
            {{ child.role }}: {{ formatNum(child.total) }}
          </div>
        </div>
        <!-- Context hit rate -->
        <div class="mb-2">
          <div class="mb-1 font-semibold text-gray-700 dark:text-gray-200">
            {{ t('sessionReport.context') }}
          </div>
          <div class="font-mono text-gray-500 dark:text-gray-400">
            {{
              t('sessionReport.hitRate', {
                pct: (report.context_hit_rate.hit_rate * 100).toFixed(0),
                e: report.context_hit_rate.effective,
                w: report.context_hit_rate.wasted
              })
            }}
          </div>
        </div>
        <!-- Code intel -->
        <div>
          <div class="mb-1 font-semibold text-gray-700 dark:text-gray-200">
            {{ t('sessionReport.codeIntel') }}
          </div>
          <div class="font-mono text-gray-500 dark:text-gray-400">
            {{
              t('sessionReport.ratio', {
                r: report.code_intel.lsp_vs_grep_ratio === null ? '—' : report.code_intel.lsp_vs_grep_ratio.toFixed(2)
              })
            }}
          </div>
          <div
            v-for="(d, i) in report.code_intel.degraded_calls"
            :key="i"
            class="text-amber-600 dark:text-amber-400">
            {{ d.tool }}: {{ d.reason }}
          </div>
        </div>
        <button
          type="button"
          class="mt-2 cursor-pointer text-[11px] text-[#2563EB] dark:text-[#60A5FA]"
          data-test="session-report-refresh"
          @click="load(true)">
          {{ t('sessionReport.refresh') }}
        </button>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useI18n } from 'vue-i18n';
import type { SessionReport } from '~/composables/bridge/session-report';
// Standalone on purpose: keeping the report bridge out of session.ts preserves the
// `vi.mock('../bridge')` seam the queued/HITL passthrough tests rely on.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchSessionReport } from '~/composables/bridge/session-report';

const { t } = useI18n();
const route = useRoute();
const sessionId = ref(typeof route.params.sid === 'string' ? route.params.sid : '');
const report = ref<SessionReport | null>(null);
const loading = ref(false);
const expanded = ref(false);

const formatNum = (n: number): string => n.toLocaleString();

const load = async (refresh = false): Promise<void> => {
  if (!sessionId.value) return;
  loading.value = true;
  try {
    report.value = await fetchSessionReport(sessionId.value, refresh);
  } catch {
    report.value = null;
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  void load();
});
watch(sessionId, () => {
  void load();
});
</script>

<i18n lang="json">
{
  "zh": {
    "sessionReport": {
      "title": "本次运行报告",
      "loading": "读取中…",
      "empty": "暂无运行数据",
      "tokens": "Token 用量",
      "grandTotal": "总计 {n} tokens",
      "main": "主 Agent：输入 {i} / 输出 {o}",
      "context": "上下文命中率",
      "hitRate": "{pct}% 命中（有效 {e} / 浪费 {w}）",
      "codeIntel": "代码检索",
      "ratio": "精确/粗暴检索比 {r}",
      "refresh": "重新生成"
    }
  },
  "en": {
    "sessionReport": {
      "title": "Session run report",
      "loading": "Loading…",
      "empty": "No run data yet",
      "tokens": "Tokens",
      "grandTotal": "Total {n} tokens",
      "main": "Main agent: in {i} / out {o}",
      "context": "Context hit rate",
      "hitRate": "{pct}% hit (effective {e} / wasted {w})",
      "codeIntel": "Code intel",
      "ratio": "Precise/greedy ratio {r}",
      "refresh": "Regenerate"
    }
  },
  "ja": {
    "sessionReport": {
      "title": "セッション実行レポート",
      "loading": "読み込み中…",
      "empty": "実行データがありません",
      "tokens": "トークン使用量",
      "grandTotal": "合計 {n} トークン",
      "main": "メインエージェント: 入力 {i} / 出力 {o}",
      "context": "コンテキスト命中率",
      "hitRate": "{pct}% 命中（有効 {e} / 無駄 {w}）",
      "codeIntel": "コード検索",
      "ratio": "精密/粗い検索比 {r}",
      "refresh": "再生成"
    }
  },
  "ko": {
    "sessionReport": {
      "title": "세션 실행 보고서",
      "loading": "불러오는 중…",
      "empty": "실행 데이터가 없습니다",
      "tokens": "토큰 사용량",
      "grandTotal": "총 {n} 토큰",
      "main": "메인 에이전트: 입력 {i} / 출력 {o}",
      "context": "컨텍스트 적중률",
      "hitRate": "{pct}% 적중 (유효 {e} / 낭비 {w})",
      "codeIntel": "코드 검색",
      "ratio": "정밀/단순 검색 비율 {r}",
      "refresh": "다시 생성"
    }
  }
}
</i18n>
