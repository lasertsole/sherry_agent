<template>
  <!-- 系统配置 → 记忆设置: the UI-editable limits (stored in the memory DB) plus
       the usage-aging trigger. Semantic recall is declared but Phase 5 is not
       implemented, so its controls stay disabled and say so. -->
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
        v-if="consolidated !== null"
        class="text-xs text-gray-500 dark:text-gray-400">
        {{ t('memorySettings.consolidated', { n: consolidated }) }}
      </span>
    </div>

    <div class="flex flex-col gap-2 rounded-lg border border-dashed border-gray-200 p-2 dark:border-gray-700">
      <div class="flex items-center gap-2">
        <ToggleSwitch
          :model-value="false"
          disabled />
        <span class="text-xs text-gray-500 dark:text-gray-400">
          {{ t('memorySettings.semanticRecall') }}
        </span>
      </div>
      <span class="text-[11px] text-gray-400">{{ t('memorySettings.semanticRecallHint') }}</span>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';
import type { MemorySettings, MemoryStats } from '~/composables/bridge';

const { t } = useI18n({ useScope: 'local' });

const DEFAULT_FORM: Pick<
  MemorySettings,
  'memory_char_limit' | 'user_char_limit' | 'facts_char_limit' | 'max_unused_days'
> = {
  memory_char_limit: 2200,
  user_char_limit: 1375,
  facts_char_limit: 1375,
  max_unused_days: 90
};
const form = ref({ ...DEFAULT_FORM });
const original = ref({ ...DEFAULT_FORM });
const stats = ref<MemoryStats | null>(null);
const saving = ref(false);
const consolidating = ref(false);
const consolidated = ref<number | null>(null);
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
    max_unused_days: settings.max_unused_days
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
  consolidating.value = true;
  error.value = '';
  try {
    const result = await consolidateMemory();
    consolidated.value = result.archived;
    stats.value = result.stats;
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
      "consolidated": "已归档 {n} 条",
      "semanticRecall": "语义召回（预留）",
      "semanticRecallHint": "Phase 5 尚未实现；开关保持关闭。"
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
      "consolidated": "{n} entries archived",
      "semanticRecall": "Semantic recall (reserved)",
      "semanticRecallHint": "Phase 5 is not implemented yet; the switch stays off."
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
      "consolidated": "{n} 件をアーカイブしました",
      "semanticRecall": "意味検索（予約）",
      "semanticRecallHint": "Phase 5 は未実装のため、スイッチはオフのままです。"
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
      "consolidated": "{n}개 보관됨",
      "semanticRecall": "의미 검색(예약)",
      "semanticRecallHint": "Phase 5는 아직 구현되지 않아 스위치가 꺼져 있습니다."
    }
  }
}
</i18n>
