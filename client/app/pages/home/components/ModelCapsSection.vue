<template>
  <div
    class="flex flex-col gap-2 rounded-lg border border-gray-100 p-3 dark:border-gray-800"
    data-test="model-caps">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="m-0 text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('config.modelCaps.title') }}
        </p>
        <p class="m-0 mt-1 text-xs text-gray-500 dark:text-gray-400">
          {{ t('config.modelCaps.hint') }}
        </p>
      </div>
      <Button
        v-if="entries.length"
        :label="t('config.modelCaps.clearAll')"
        icon="pi pi-trash"
        size="small"
        text
        severity="secondary"
        :loading="busy"
        data-test="model-caps-clear-all"
        @click="onClearAll" />
    </div>

    <p
      v-if="error"
      class="m-0 text-xs text-red-600 dark:text-red-400">
      {{ error }}
    </p>
    <p
      v-else-if="loading"
      class="m-0 text-xs text-gray-400">
      {{ t('config.modelCaps.loading') }}
    </p>
    <p
      v-else-if="entries.length === 0"
      class="m-0 text-xs text-gray-400">
      {{ t('config.modelCaps.empty') }}
    </p>

    <div
      v-for="entry in entries"
      :key="`${entry.provider}/${entry.model}`"
      class="flex items-start justify-between gap-3 rounded-md bg-gray-50 p-2 dark:bg-gray-900/40"
      data-test="model-caps-row">
      <div class="min-w-0">
        <p class="m-0 break-all font-mono text-xs text-gray-700 dark:text-gray-200">
          {{ entry.provider }}/{{ entry.model }}
        </p>
        <p class="m-0 mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
          {{
            t('config.modelCaps.level', {
              stored: entry.level,
              effective: entry.effective_level
            })
          }}
        </p>
        <p
          v-if="entry.reason"
          class="m-0 mt-1 break-words text-[11px] text-gray-400">
          {{ entry.reason }}
        </p>
      </div>
      <Button
        icon="pi pi-trash"
        text
        size="small"
        severity="danger"
        :aria-label="t('config.modelCaps.clearOne')"
        :disabled="busy"
        data-test="model-caps-clear-one"
        @click="onClear(entry)" />
    </div>
  </div>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';
import type { ModelCapEntry } from '@/composables/model-caps';

const { t } = useI18n();

const entries = ref<ModelCapEntry[]>([]);
const loading = ref(true);
const busy = ref(false);
const error = ref('');

async function load(): Promise<void> {
  loading.value = true;
  try {
    entries.value = await readModelCaps();
    error.value = '';
  } catch {
    error.value = t('config.modelCaps.loadFailed');
  } finally {
    loading.value = false;
  }
}

async function applyClear(ok: boolean): Promise<void> {
  if (!ok) {
    error.value = t('config.modelCaps.clearFailed');
    return;
  }
  await load();
}

async function onClear(entry: ModelCapEntry): Promise<void> {
  busy.value = true;
  try {
    await applyClear(await clearModelCap(entry.provider, entry.model));
  } finally {
    busy.value = false;
  }
}

async function onClearAll(): Promise<void> {
  busy.value = true;
  try {
    await applyClear(await clearAllModelCaps());
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "modelCaps": {
        "title": "结构化输出降级",
        "hint": "绑定 schema 的子代理在 provider 拒绝强制 tool_choice 时会自动降级；重探间隔到达后会自动回退一级。清除条目可让下一次 spawn 立即重试默认策略。",
        "empty": "暂无降级模型。",
        "loading": "读取中…",
        "level": "持久级别 {stored} · 下次使用 {effective}",
        "clearAll": "全部清除",
        "clearOne": "清除此条",
        "loadFailed": "降级记录读取失败。",
        "clearFailed": "清除失败，请重试。"
      }
    }
  },
  "en": {
    "config": {
      "modelCaps": {
        "title": "Structured-output degradation",
        "hint": "Schema-bound subagents fall back when a provider rejects the forced tool_choice; the level decays one rung after the re-probe interval. Clear an entry to retry the default strategy on the next spawn.",
        "empty": "No degraded models.",
        "loading": "Loading…",
        "level": "Stored level {stored} · next spawn uses {effective}",
        "clearAll": "Clear all",
        "clearOne": "Clear this entry",
        "loadFailed": "Could not load the degradation list.",
        "clearFailed": "Clear failed; try again."
      }
    }
  },
  "ja": {
    "config": {
      "modelCaps": {
        "title": "構造化出力の降格",
        "hint": "スキーマ束縛のサブエージェントは、provider が強制 tool_choice を拒否すると自動で降格します。再試行間隔を過ぎると 1 段階戻ります。項目を消去すると次回の spawn で既定の戦略を再試行します。",
        "empty": "降格されたモデルはありません。",
        "loading": "読み込み中…",
        "level": "保存レベル {stored} · 次回使用 {effective}",
        "clearAll": "すべて消去",
        "clearOne": "この項目を消去",
        "loadFailed": "降格一覧を読み込めませんでした。",
        "clearFailed": "消去に失敗しました。再試行してください。"
      }
    }
  },
  "ko": {
    "config": {
      "modelCaps": {
        "title": "구조화 출력 강등",
        "hint": "스키마 바인딩 서브에이전트는 provider가 강제 tool_choice를 거부하면 자동으로 강등됩니다. 재시도 간격이 지나면 한 단계 되돌아갑니다. 항목을 지우면 다음 spawn에서 기본 전략을 다시 시도합니다.",
        "empty": "강등된 모델이 없습니다.",
        "loading": "불러오는 중…",
        "level": "저장 레벨 {stored} · 다음 사용 {effective}",
        "clearAll": "모두 지우기",
        "clearOne": "이 항목 지우기",
        "loadFailed": "강등 목록을 불러오지 못했습니다.",
        "clearFailed": "지우지 못했습니다. 다시 시도하세요."
      }
    }
  }
}
</i18n>
