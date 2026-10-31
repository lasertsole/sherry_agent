<template>
  <!-- 系统配置 → 直播安全. Everything here applies LIVE (the backend reads
       sherry.jsonc through an mtime cache), so the switches need no restart. -->
  <div class="flex flex-col gap-4">
    <p
      v-if="error"
      class="m-0 text-sm text-red-600 dark:text-red-400">
      {{ error }}
    </p>

    <!-- Master switch -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('safetyFilter.master') }}
        </span>
        <ToggleSwitch
          :model-value="enabled"
          data-test="safety-master-switch"
          :aria-label="t('safetyFilter.master')"
          @update:model-value="onMasterChange" />
      </div>
      <small class="text-xs text-gray-400 dark:text-gray-500">{{ t('safetyFilter.masterHint') }}</small>
    </section>

    <!-- LLM moderation. The switch is unavailable until a review model is set:
         the backend ALSO refuses to run the review without one. -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('safetyFilter.llm') }}
        </span>
        <ToggleSwitch
          :model-value="llmOn"
          data-test="safety-llm-switch"
          :disabled="!llmModel.trim()"
          :aria-label="t('safetyFilter.llm')"
          @update:model-value="onLlmSwitchChange" />
      </div>
      <small class="text-xs text-gray-400 dark:text-gray-500">
        {{ llmModel.trim() ? t('safetyFilter.llmHint') : t('safetyFilter.llmModelRequired') }}
      </small>
      <label class="text-xs text-gray-500 dark:text-gray-400">{{ t('safetyFilter.llmModel') }}</label>
      <InputText
        v-model="llmModel"
        class="w-full font-mono text-xs"
        data-test="safety-llm-model"
        :placeholder="t('safetyFilter.llmModelPlaceholder')"
        autocomplete="off"
        spellcheck="false"
        @change="saveSwitches" />
      <label class="text-xs text-gray-500 dark:text-gray-400">{{ t('safetyFilter.llmMode') }}</label>
      <Select
        :model-value="llmMode"
        :options="modeOptions"
        option-label="label"
        option-value="value"
        class="w-full text-xs"
        data-test="safety-llm-mode"
        :disabled="!llmOn"
        @update:model-value="onModeChange" />
    </section>

    <!-- Keyword rules: the file is operator-owned; the editor writes it whole. -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('safetyFilter.rules') }}
        </span>
        <span class="text-xs text-gray-500 dark:text-gray-400">
          {{ t('safetyFilter.rulesCount', { count: rulesCount }) }}
        </span>
      </div>
      <small class="text-xs text-gray-400 dark:text-gray-500">{{ t('safetyFilter.rulesHint') }}</small>
      <small class="font-mono text-[0.7rem] text-gray-400">{{ rulesPath }}</small>
      <Textarea
        v-model="rulesText"
        class="w-full font-mono text-xs"
        rows="10"
        auto-resize
        data-test="safety-rules-editor"
        :aria-label="t('safetyFilter.rules')" />
      <div class="flex flex-wrap items-center gap-2">
        <Button
          :label="t('safetyFilter.saveRules')"
          icon="pi pi-check"
          size="small"
          :loading="saving"
          data-test="safety-rules-save"
          @click="saveRules" />
        <span class="text-xs text-gray-400">{{ t('safetyFilter.rulesFormat') }}</span>
      </div>
    </section>

    <!-- Test one piece of text against the loaded rules. -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
        {{ t('safetyFilter.test') }}
      </span>
      <div class="flex flex-wrap items-center gap-2">
        <InputText
          v-model="testText"
          class="flex-1 min-w-40 text-xs"
          data-test="safety-test-input"
          :placeholder="t('safetyFilter.testPlaceholder')"
          autocomplete="off"
          spellcheck="false" />
        <Button
          :label="t('safetyFilter.testRun')"
          icon="pi pi-search"
          size="small"
          text
          data-test="safety-test-run"
          :disabled="!testText.trim()"
          @click="runTest" />
      </div>
      <ul
        v-if="testHits.length"
        class="m-0 flex list-none flex-col gap-1 p-0"
        data-test="safety-test-hits">
        <li
          v-for="(hit, index) in testHits"
          :key="index"
          class="flex items-center gap-2 rounded px-1 py-1 text-xs">
          <span class="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 text-[0.65rem] text-red-500">{{ hit.action }}</span>
          <span class="min-w-0 flex-1 truncate font-mono">{{ hit.pattern }}</span>
          <span class="shrink-0 truncate text-gray-400">{{ hit.matched_text }}</span>
        </li>
      </ul>
      <small
        v-else-if="tested"
        class="text-xs text-gray-400"
        data-test="safety-test-empty">
        {{ t('safetyFilter.testEmpty') }}
      </small>
    </section>

    <!-- Recent filter events (process-level ring; not persisted). -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('safetyFilter.log') }}
        </span>
        <Button
          icon="pi pi-refresh"
          size="small"
          text
          severity="secondary"
          :aria-label="t('safetyFilter.refresh')"
          data-test="safety-log-refresh"
          @click="loadLog" />
      </div>
      <ul
        v-if="logEvents.length"
        class="m-0 flex max-h-48 list-none flex-col gap-1 overflow-y-auto p-0"
        data-test="safety-log-list">
        <li
          v-for="(event, index) in logEvents"
          :key="index"
          class="flex items-center gap-2 rounded px-1 py-1 text-xs text-gray-600 dark:text-gray-300">
          <span class="shrink-0 font-mono text-[0.65rem] text-gray-400">{{ formatTime(event.at) }}</span>
          <span class="shrink-0 rounded bg-gray-500/15 px-1.5 py-0.5 text-[0.65rem]">{{ event.layer }}</span>
          <span class="min-w-0 flex-1 truncate">{{ event.message }}</span>
        </li>
      </ul>
      <small
        v-else
        class="text-xs text-gray-400">
        {{ t('safetyFilter.logEmpty') }}
      </small>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';

import type { SafetyLogEvent, SafetyRule, SafetyTestHit } from '~/composables/safetyFilter';

/**
 * The live-stream safety tab (系统配置 → 直播安全).
 *
 * Two halves with different storage: the switches are `sherry.jsonc` keys (saved
 * through the App Config writer, read live by the backend), the keyword RULES are
 * the operator's own file (its own endpoint, validated there). The LLM switch is
 * disabled until a review model is typed — the backend independently refuses to
 * run a review without one.
 */
const { t } = useI18n({ useScope: 'local' });

const MODE_KEYWORDS = 'on_keyword_hit';
const MODE_ALWAYS = 'always';

const enabled = ref(false);
const llmOn = ref(false);
const llmMode = ref<string>(MODE_KEYWORDS);
const llmModel = ref('');

const rulesText = ref('{\n  "rules": []\n}');
const rulesCount = ref(0);
const rulesPath = ref('');
const saving = ref(false);

const testText = ref('');
const testHits = ref<SafetyTestHit[]>([]);
const tested = ref(false);

const logEvents = ref<SafetyLogEvent[]>([]);
const error = ref('');

/** The LLM review strategy options (the SELECT the switch reveals). */
const modeOptions = [
  { label: t('safetyFilter.modeKeywordHit'), value: MODE_KEYWORDS },
  { label: t('safetyFilter.modeAlways'), value: MODE_ALWAYS }
];

/** Format one event timestamp as a local time-of-day. */
const formatTime = (at: number): string => new Date(at * 1000).toLocaleTimeString();

/** Master switch: apply the new value first, then persist. */
const onMasterChange = (value: unknown): void => {
  enabled.value = Boolean(value);
  void saveSwitches();
};

/** LLM switch: off writes the `off` mode; on restores the chosen strategy. */
const onLlmSwitchChange = (value: unknown): void => {
  llmOn.value = Boolean(value);
  void saveSwitches();
};

/** Strategy select (only meaningful while the LLM switch is on). */
const onModeChange = (value: unknown): void => {
  llmMode.value = String(value);
  void saveSwitches();
};

/** Persist the three switches/mode/model; the backend applies them live. */
const saveSwitches = async (): Promise<void> => {
  error.value = '';
  try {
    await writeSherryConfig({
      'safety_filter.enabled': enabled.value ? 'true' : 'false',
      'safety_filter.llm_moderation_mode': llmOn.value ? llmMode.value : 'off',
      'safety_filter.llm_moderation_model': llmModel.value.trim()
    });
  } catch {
    error.value = t('safetyFilter.saveError');
  }
};

/** Load the switches from the App Config registry. */
const loadSettings = async (): Promise<void> => {
  try {
    const payload = await readSherryConfig();
    const byKey = new Map((payload.entries || []).map(entry => [entry.key, entry.value]));
    enabled.value = String(byKey.get('safety_filter.enabled') ?? 'false').toLowerCase() === 'true';
    const mode = String(byKey.get('safety_filter.llm_moderation_mode') ?? 'off');
    llmOn.value = mode !== 'off';
    llmMode.value = mode === MODE_ALWAYS ? MODE_ALWAYS : MODE_KEYWORDS;
    llmModel.value = String(byKey.get('safety_filter.llm_moderation_model') ?? '');
  } catch {
    error.value = t('safetyFilter.loadError');
  }
};

/** Load the rules file into the editor. */
const loadRules = async (): Promise<void> => {
  try {
    const snapshot = await readSafetyRules();
    rulesCount.value = snapshot.count;
    rulesPath.value = snapshot.path;
    rulesText.value = JSON.stringify(
      {
        rules: snapshot.rules.map((rule: SafetyRule) => ({
          pattern: rule.pattern,
          type: rule.type,
          action: rule.action,
          scope: rule.scope,
          replacement: rule.replacement
        }))
      },
      null,
      2
    );
  } catch {
    error.value = t('safetyFilter.loadError');
  }
};

/** Validate + write the edited rules. */
const saveRules = async (): Promise<void> => {
  error.value = '';
  let parsed: unknown;
  try {
    parsed = JSON.parse(rulesText.value);
  } catch {
    error.value = t('safetyFilter.rulesJsonInvalid');
    return;
  }
  const rules = (parsed as { rules?: SafetyRule[] })?.rules;
  if (!Array.isArray(rules)) {
    error.value = t('safetyFilter.rulesJsonInvalid');
    return;
  }
  saving.value = true;
  try {
    const result = await writeSafetyRules(rules);
    if (!result.success) {
      error.value = result.message || t('safetyFilter.rulesWriteFailed');
      return;
    }
    await loadRules();
    toastSuccess(t('safetyFilter.rulesSaved'));
  } catch {
    error.value = t('safetyFilter.rulesWriteFailed');
  } finally {
    saving.value = false;
  }
};

/** Scan the sample text (output scope) against the loaded rules. */
const runTest = async (): Promise<void> => {
  tested.value = false;
  try {
    testHits.value = await testSafetyRules(testText.value, 'output');
    tested.value = true;
  } catch {
    error.value = t('safetyFilter.loadError');
  }
};

/** Pull the recent filter events. */
async function loadLog(): Promise<void> {
  try {
    logEvents.value = await readSafetyLog(undefined, 50);
  } catch {
    error.value = t('safetyFilter.loadError');
  }
}

onMounted(() => {
  void loadSettings();
  void loadRules();
  void loadLog();
});
</script>

<i18n lang="json">
{
  "zh": {
    "safetyFilter": {
      "master": "直播安全过滤",
      "masterHint": "开启后：输入/输出按关键词规则过滤，流式输出命中即截断（先扫描再发送）。默认关闭，关闭时零开销。",
      "llm": "LLM 内容审核",
      "llmHint": "命中关键词后再调用审核模型（或每条都审）；模型判定不安全时直接拒绝。",
      "llmModelRequired": "未配置审核模型：LLM 审核不会启用。先填写模型名再打开开关。",
      "llmModel": "审核模型",
      "llmModelPlaceholder": "例如 deepseek-chat（留空 = 不启用）",
      "llmMode": "审核时机",
      "modeKeywordHit": "仅关键词命中时",
      "modeAlways": "每条都审核",
      "rules": "关键词规则",
      "rulesHint": "规则文件由你维护，应用不内置任何词表。type: exact / contains / regex；action: block / replace / warn；scope: input / output / both。保存后立即生效。",
      "rulesCount": "已加载 {count} 条规则",
      "rulesFormat": "格式：{\"rules\": [{\"pattern\": \"…\", \"type\": \"contains\", \"action\": \"block\", \"scope\": \"both\"}]}",
      "saveRules": "保存规则",
      "rulesSaved": "关键词规则已保存",
      "rulesError": "关键词规则保存失败",
      "rulesWriteFailed": "关键词规则保存失败",
      "rulesJsonInvalid": "JSON 格式不合法（需要 {\"rules\": [...]}）",
      "test": "规则测试",
      "testPlaceholder": "输入一段文本，查看会命中哪些输出规则",
      "testRun": "测试",
      "testEmpty": "没有命中任何规则",
      "log": "最近过滤记录",
      "logEmpty": "暂无过滤记录",
      "refresh": "刷新",
      "loadError": "读取失败，请检查后端服务",
      "saveError": "保存失败，请检查后端服务"
    }
  },
  "en": {
    "safetyFilter": {
      "master": "Live-stream safety filter",
      "masterHint": "When on, input and output are filtered by your keyword rules and a blocked stream is cut before the text is sent. Off by default — no cost while off.",
      "llm": "LLM content review",
      "llmHint": "After a keyword hit (or on every message, per the strategy) the review model judges the message; unsafe input is refused.",
      "llmModelRequired": "No review model configured: LLM review stays off. Type a model name before switching it on.",
      "llmModel": "Review model",
      "llmModelPlaceholder": "e.g. deepseek-chat (empty = disabled)",
      "llmMode": "Review timing",
      "modeKeywordHit": "Only after a keyword hit",
      "modeAlways": "Every message",
      "rules": "Keyword rules",
      "rulesHint": "You own the rules file; no word list ships with the app. type: exact / contains / regex; action: block / replace / warn; scope: input / output / both. Saving applies immediately.",
      "rulesCount": "{count} rule(s) loaded",
      "rulesFormat": "Format: {\"rules\": [{\"pattern\": \"…\", \"type\": \"contains\", \"action\": \"block\", \"scope\": \"both\"}]}",
      "saveRules": "Save rules",
      "rulesSaved": "Keyword rules saved",
      "rulesError": "Could not save the keyword rules",
      "rulesWriteFailed": "Could not save the keyword rules",
      "rulesJsonInvalid": "Invalid JSON (expected {\"rules\": [...]})",
      "test": "Rule test",
      "testPlaceholder": "Type text to see which output rules it hits",
      "testRun": "Test",
      "testEmpty": "No rule matched",
      "log": "Recent filter events",
      "logEmpty": "No filter events yet",
      "refresh": "Refresh",
      "loadError": "Load failed — check the backend service",
      "saveError": "Save failed — check the backend service"
    }
  },
  "ja": {
    "safetyFilter": {
      "master": "ライブ配信の安全フィルター",
      "masterHint": "オンにすると入出力がキーワード ルールで絞り込まれ、ブロックされたストリームは送信前に切断されます。既定はオフで、オフの間はコストがかかりません。",
      "llm": "LLM 内容審査",
      "llmHint": "キーワードに一致した後（戦略によっては毎回）審査モデルが判定し、安全でない入力は拒否します。",
      "llmModelRequired": "審査モデルが未設定のため LLM 審査は有効になりません。モデル名を入力してからオンにしてください。",
      "llmModel": "審査モデル",
      "llmModelPlaceholder": "例: deepseek-chat（空 = 無効）",
      "llmMode": "審査のタイミング",
      "modeKeywordHit": "キーワード一致時のみ",
      "modeAlways": "毎回",
      "rules": "キーワード ルール",
      "rulesHint": "ルール ファイルはご自身で管理します（語リストは同梱していません）。type: exact / contains / regex、action: block / replace / warn、scope: input / output / both。保存するとすぐ反映されます。",
      "rulesCount": "{count} 件のルールを読み込み済み",
      "rulesFormat": "形式: {\"rules\": [{\"pattern\": \"…\", \"type\": \"contains\", \"action\": \"block\", \"scope\": \"both\"}]}",
      "saveRules": "ルールを保存",
      "rulesSaved": "キーワード ルールを保存しました",
      "rulesError": "キーワード ルールを保存できませんでした",
      "rulesWriteFailed": "キーワード ルールを保存できませんでした",
      "rulesJsonInvalid": "JSON が不正です（{\"rules\": [...]} が必要）",
      "test": "ルールのテスト",
      "testPlaceholder": "テキストを入力すると、一致する出力ルールを表示します",
      "testRun": "テスト",
      "testEmpty": "一致するルールはありません",
      "log": "最近のフィルター記録",
      "logEmpty": "フィルター記録はまだありません",
      "refresh": "更新",
      "loadError": "読み込みに失敗しました。バックエンドを確認してください",
      "saveError": "保存に失敗しました。バックエンドを確認してください"
    }
  },
  "ko": {
    "safetyFilter": {
      "master": "라이브 안전 필터",
      "masterHint": "켜면 입력과 출력이 키워드 규칙으로 걸러지고, 차단된 스트림은 전송 전에 잘립니다. 기본은 꺼짐이며 꺼져 있는 동안 비용이 없습니다.",
      "llm": "LLM 콘텐츠 심사",
      "llmHint": "키워드가 걸린 뒤(전략에 따라 매번) 심사 모델이 판단하며, 안전하지 않은 입력은 거부됩니다.",
      "llmModelRequired": "심사 모델이 없어 LLM 심사가 켜지지 않습니다. 모델 이름을 입력한 뒤 스위치를 켜세요.",
      "llmModel": "심사 모델",
      "llmModelPlaceholder": "예: deepseek-chat (비우면 비활성)",
      "llmMode": "심사 시점",
      "modeKeywordHit": "키워드 적중 시에만",
      "modeAlways": "매번",
      "rules": "키워드 규칙",
      "rulesHint": "규칙 파일은 사용자가 관리합니다(단어 목록은 포함되지 않음). type: exact / contains / regex, action: block / replace / warn, scope: input / output / both. 저장하면 즉시 적용됩니다.",
      "rulesCount": "{count}개 규칙 로드됨",
      "rulesFormat": "형식: {\"rules\": [{\"pattern\": \"…\", \"type\": \"contains\", \"action\": \"block\", \"scope\": \"both\"}]}",
      "saveRules": "규칙 저장",
      "rulesSaved": "키워드 규칙을 저장했습니다",
      "rulesError": "키워드 규칙을 저장하지 못했습니다",
      "rulesWriteFailed": "키워드 규칙을 저장하지 못했습니다",
      "rulesJsonInvalid": "JSON 형식이 잘못되었습니다({\"rules\": [...]} 필요)",
      "test": "규칙 테스트",
      "testPlaceholder": "텍스트를 입력하면 어떤 출력 규칙에 걸리는지 보여 줍니다",
      "testRun": "테스트",
      "testEmpty": "일치하는 규칙이 없습니다",
      "log": "최근 필터 기록",
      "logEmpty": "아직 필터 기록이 없습니다",
      "refresh": "새로고침",
      "loadError": "불러오지 못했습니다. 백엔드 서비스를 확인하세요",
      "saveError": "저장하지 못했습니다. 백엔드 서비스를 확인하세요"
    }
  }
}
</i18n>
