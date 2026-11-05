<template>
  <!-- 系统配置 → 记忆设置: the UI-editable limits (stored in the memory DB), the
       semantic-recall switch and the async consolidation trigger. -->
  <div class="flex flex-col gap-4">
    <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{ t('memorySettings.hint') }}
    </p>

    <div
      v-if="error"
      class="text-sm text-red-600 dark:text-red-400">
      {{ error }}
    </div>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.memoryLimit') }}
        </label>
        <InputNumber
          v-model="form.memory_char_limit"
          :min="1"
          :max="8000"
          :use-grouping="false"
          class="w-full" />
      </div>
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.userLimit') }}
        </label>
        <InputNumber
          v-model="form.user_char_limit"
          :min="1"
          :max="8000"
          :use-grouping="false"
          class="w-full" />
      </div>
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.factsLimit') }}
        </label>
        <InputNumber
          v-model="form.facts_char_limit"
          :min="1"
          :max="8000"
          :use-grouping="false"
          class="w-full" />
      </div>
      <div class="flex flex-col gap-1">
        <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.maxUnusedDays') }}
        </label>
        <InputNumber
          v-model="form.max_unused_days"
          :min="1"
          :max="3650"
          :use-grouping="false"
          class="w-full" />
        <span class="text-[11px] text-gray-400">{{ t('memorySettings.maxUnusedDaysHint') }}</span>
      </div>
    </div>

    <div
      v-if="stats"
      class="rounded-lg border border-gray-100 p-2 text-xs text-gray-500 dark:border-gray-800 dark:text-gray-400">
      {{
        t('memorySettings.usage', {
          entries: stats.total_entries,
          chars: stats.total_chars,
          percent: totalPercent
        })
      }}
    </div>

    <div class="flex flex-col gap-2 rounded-lg border border-dashed border-gray-200 p-3 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <ToggleSwitch
          v-model="form.semantic_recall_enabled"
          data-test="memory-semantic-toggle" />
        <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
          {{ t('memorySettings.semanticRecall') }}
        </span>
      </div>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div class="flex flex-col gap-1">
          <label class="text-xs text-gray-600 dark:text-gray-300">
            {{ t('memorySettings.semanticTopK') }}
          </label>
          <InputNumber
            v-model="form.semantic_recall_top_k"
            :min="1"
            :max="20"
            :use-grouping="false"
            :disabled="!form.semantic_recall_enabled"
            data-test="memory-semantic-top-k"
            class="w-full" />
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-xs text-gray-600 dark:text-gray-300">
            {{ t('memorySettings.semanticBudget') }}
          </label>
          <InputNumber
            v-model="form.semantic_recall_char_budget"
            :min="200"
            :max="20000"
            :use-grouping="false"
            :disabled="!form.semantic_recall_enabled"
            data-test="memory-semantic-budget"
            class="w-full" />
        </div>
      </div>
      <span class="text-[11px] text-gray-400">{{ t('memorySettings.semanticRecallHint') }}</span>
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <Button
        :label="t('memorySettings.save')"
        icon="pi pi-check"
        size="small"
        :loading="saving"
        :disabled="!dirty"
        data-test="memory-settings-save"
        @click="handleSave" />
      <Button
        :label="t('memorySettings.consolidate')"
        icon="pi pi-filter-slash"
        size="small"
        severity="secondary"
        :loading="consolidating"
        data-test="memory-consolidate"
        @click="handleConsolidate" />
      <span
        v-if="consolidationMessage"
        class="text-xs text-gray-500 dark:text-gray-400">
        {{ consolidationMessage }}
      </span>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import { logUtil } from '~/utils/log';
import type { MemorySettings, MemoryStats } from '~/composables/bridge';

const { t } = useI18n({ useScope: 'local' });
const route = useRoute() as unknown as { params?: Record<string, unknown> } | undefined;

/** The session the manual consolidation pass runs under (model + prompt). */
const sessionId = computed(() => String(route?.params?.sid ?? ''));

const DEFAULT_FORM: Pick<
  MemorySettings,
  | 'memory_char_limit'
  | 'user_char_limit'
  | 'facts_char_limit'
  | 'max_unused_days'
  | 'semantic_recall_enabled'
  | 'semantic_recall_top_k'
  | 'semantic_recall_char_budget'
> = {
  memory_char_limit: 2200,
  user_char_limit: 1375,
  facts_char_limit: 1375,
  max_unused_days: 90,
  semantic_recall_enabled: false,
  semantic_recall_top_k: 5,
  semantic_recall_char_budget: 4000
};
const form = ref({ ...DEFAULT_FORM });
const original = ref({ ...DEFAULT_FORM });
const stats = ref<MemoryStats | null>(null);
const saving = ref(false);
const consolidating = ref(false);
const consolidationMessage = ref('');
const error = ref('');

const dirty = computed(() =>
  (Object.keys(DEFAULT_FORM) as (keyof typeof DEFAULT_FORM)[]).some(key => form.value[key] !== original.value[key])
);
const totalPercent = computed(() => {
  if (!stats.value) return 0;
  const targetPercent = Object.values(stats.value.targets).map(target => target.percent);
  return targetPercent.length
    ? Math.round(targetPercent.reduce((sum, value) => sum + value, 0) / targetPercent.length)
    : 0;
});

function apply(settings: MemorySettings) {
  form.value = {
    memory_char_limit: settings.memory_char_limit,
    user_char_limit: settings.user_char_limit,
    facts_char_limit: settings.facts_char_limit,
    max_unused_days: settings.max_unused_days,
    semantic_recall_enabled: settings.semantic_recall_enabled,
    semantic_recall_top_k: settings.semantic_recall_top_k,
    semantic_recall_char_budget: settings.semantic_recall_char_budget
  };
  original.value = { ...form.value };
}

async function load() {
  error.value = '';
  try {
    const [settings, currentStats] = await Promise.all([readMemorySettings(), readMemoryStats()]);
    apply(settings);
    stats.value = currentStats;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    logUtil.e('[MemorySettingsSection] Failed to load:', e);
  }
}

async function handleSave() {
  saving.value = true;
  error.value = '';
  try {
    const saved = await writeMemorySettings({ ...form.value });
    apply(saved);
    stats.value = await readMemoryStats();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
  }
}

async function handleConsolidate() {
  if (!sessionId.value) {
    error.value = t('memorySettings.sessionRequired');
    return;
  }
  consolidating.value = true;
  consolidationMessage.value = '';
  error.value = '';
  try {
    const started = await consolidateMemory(sessionId.value);
    let task = started;
    const deadline = Date.now() + 300_000;
    while (task.status === 'running' && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      task = await readConsolidationStatus(task.task_id);
    }
    if (task.status === 'running') {
      throw new Error(t('memorySettings.consolidationTimeout'));
    }
    if (task.status === 'failed') {
      throw new Error(task.error || t('memorySettings.consolidationFailed'));
    }
    const archived = task.result?.archived ?? 0;
    const remaining = task.result?.entries_after;
    consolidationMessage.value =
      task.status === 'skipped'
        ? t('memorySettings.consolidationSkipped', { n: archived })
        : t('memorySettings.consolidationDone', { n: archived, m: remaining ?? '?' });
    stats.value = await readMemoryStats();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    consolidating.value = false;
  }
}

onMounted(load);
</script>

<i18n lang="json">
{
  "zh": {
    "memorySettings": {
      "hint": "记忆条目保存在 SQLite 中；这里的容量上限与老化窗口立即生效。",
      "memoryLimit": "记忆容量上限（字符）",
      "userLimit": "用户画像容量上限（字符）",
      "factsLimit": "系统事实容量上限（字符）",
      "maxUnusedDays": "未使用老化天数",
      "maxUnusedDaysHint": "超过该天数未使用的条目会在整理时归档（不会删除）。",
      "usage": "当前 {entries} 条 · {chars} 字符 · 平均占用 {percent}%",
      "save": "保存记忆设置",
      "consolidate": "立即整理",
      "consolidationDone": "已整理：归档 {n} 条，剩余 {m} 条",
      "consolidationSkipped": "条目不足，仅执行老化（归档 {n} 条）",
      "consolidationFailed": "整理失败",
      "consolidationTimeout": "整理超时，请稍后重试",
      "sessionRequired": "请先打开一个会话再整理记忆",
      "semanticRecall": "语义召回",
      "semanticRecallHint": "开启后系统提示词只放记忆索引，每轮按需检索相关条目；需要 embedding 与 reranker 模型。",
      "semanticTopK": "召回条数",
      "semanticBudget": "召回字符预算"
    }
  },
  "en": {
    "memorySettings": {
      "hint": "Memory entries live in SQLite; these caps and the aging window apply immediately.",
      "memoryLimit": "Memory cap (characters)",
      "userLimit": "User-profile cap (characters)",
      "factsLimit": "System-facts cap (characters)",
      "maxUnusedDays": "Unused aging window (days)",
      "maxUnusedDaysHint": "Entries unused for longer than this are archived during consolidation (never deleted).",
      "usage": "{entries} entries · {chars} chars · {percent}% average usage",
      "save": "Save memory settings",
      "consolidate": "Consolidate now",
      "consolidationDone": "Consolidated: {n} archived, {m} remaining",
      "consolidationSkipped": "Too few entries; aging only ({n} archived)",
      "consolidationFailed": "Consolidation failed",
      "consolidationTimeout": "Consolidation timed out; try again later",
      "sessionRequired": "Open a session before consolidating memory",
      "semanticRecall": "Semantic recall",
      "semanticRecallHint": "When on, the system prompt carries only a memory index and relevant entries are recalled per turn; requires the embedding and reranker models.",
      "semanticTopK": "Recall count",
      "semanticBudget": "Recall character budget"
    }
  },
  "ja": {
    "memorySettings": {
      "hint": "記憶エントリは SQLite に保存されます。この上限と老化期間はすぐに反映されます。",
      "memoryLimit": "記憶の上限（文字数）",
      "userLimit": "ユーザープロファイルの上限（文字数）",
      "factsLimit": "システム事実の上限（文字数）",
      "maxUnusedDays": "未使用の老化日数",
      "maxUnusedDaysHint": "この日数を超えて未使用のエントリは整理時にアーカイブされます（削除されません）。",
      "usage": "現在 {entries} 件 · {chars} 文字 · 平均使用率 {percent}%",
      "save": "記憶設定を保存",
      "consolidate": "今すぐ整理",
      "consolidationDone": "整理完了：{n} 件をアーカイブ、残り {m} 件",
      "consolidationSkipped": "件数が少ないため老化のみ実行（{n} 件をアーカイブ）",
      "consolidationFailed": "整理に失敗しました",
      "consolidationTimeout": "整理がタイムアウトしました。後でもう一度お試しください",
      "sessionRequired": "記憶を整理する前にセッションを開いてください",
      "semanticRecall": "意味検索",
      "semanticRecallHint": "オンにするとシステムプロンプトは記憶インデックスのみになり、ターンごとに関連エントリを検索します。embedding と reranker モデルが必要です。",
      "semanticTopK": "検索件数",
      "semanticBudget": "検索の文字予算"
    }
  },
  "ko": {
    "memorySettings": {
      "hint": "기억 항목은 SQLite에 저장됩니다. 여기의 상한과 노화 기간은 즉시 적용됩니다.",
      "memoryLimit": "기억 용량 상한(문자)",
      "userLimit": "사용자 프로필 상한(문자)",
      "factsLimit": "시스템 사실 상한(문자)",
      "maxUnusedDays": "미사용 노화 일수",
      "maxUnusedDaysHint": "이 일수보다 오래 사용되지 않은 항목은 정리 시 보관됩니다(삭제되지 않음).",
      "usage": "현재 {entries}개 · {chars}자 · 평균 사용률 {percent}%",
      "save": "기억 설정 저장",
      "consolidate": "지금 정리",
      "consolidationDone": "정리 완료: {n}개 보관, {m}개 남음",
      "consolidationSkipped": "항목이 적어 노화만 실행({n}개 보관)",
      "consolidationFailed": "정리에 실패했습니다",
      "consolidationTimeout": "정리 시간이 초과되었습니다. 나중에 다시 시도하세요",
      "sessionRequired": "기억을 정리하기 전에 세션을 여세요",
      "semanticRecall": "의미 검색",
      "semanticRecallHint": "켜면 시스템 프롬프트에는 기억 색인만 들어가고 매 턴 관련 항목을 검색합니다. embedding과 reranker 모델이 필요합니다.",
      "semanticTopK": "검색 개수",
      "semanticBudget": "검색 문자 예산"
    }
  }
}
</i18n>
