<template>
  <div
    class="mt-3 flex flex-col gap-2 text-xs"
    data-test="thinking-custom-panel">
    <!-- Collapsed by default: this is the escape hatch for a provider the
         built-in formats do not cover, not something a normal session needs. -->
    <button
      type="button"
      class="flex w-full items-center gap-1.5 text-left text-gray-500 transition-colors hover:text-theme-main dark:text-gray-400"
      data-test="thinking-custom-toggle"
      @click="open = !open">
      <i :class="['pi text-[10px]', open ? 'pi-chevron-down' : 'pi-chevron-right']" />
      <span>{{ t('thinkingCustom.title') }}</span>
      <span
        v-if="saved"
        class="rounded-full bg-theme-main/15 px-1.5 py-0.5 text-[10px] text-theme-main"
        data-test="thinking-custom-active">
        {{ t('thinkingCustom.active') }}
      </span>
    </button>

    <div
      v-if="open"
      class="flex flex-col gap-2 rounded border border-gray-200 p-2 dark:border-gray-700">
      <p
        class="text-gray-400"
        data-test="thinking-custom-hint">
        {{ t('thinkingCustom.hint') }}
      </p>

      <label class="flex flex-col gap-1">
        <span class="text-gray-500 dark:text-gray-400">{{ t('thinkingCustom.wireField') }}</span>
        <InputText
          v-model="draft.wire_field"
          class="w-full"
          :placeholder="t('thinkingCustom.wireFieldPlaceholder')"
          data-test="thinking-custom-wire-field" />
      </label>

      <label class="flex flex-col gap-1">
        <span class="text-gray-500 dark:text-gray-400">{{ t('thinkingCustom.wireFormat') }}</span>
        <Select
          v-model="draft.wire_format"
          :options="FORMAT_OPTIONS"
          option-label="label"
          option-value="value"
          class="w-full"
          data-test="thinking-custom-wire-format" />
      </label>

      <div class="flex flex-col gap-1">
        <span class="text-gray-500 dark:text-gray-400">{{ t('thinkingCustom.levels') }}</span>
        <div
          v-for="level in LEVELS"
          :key="level"
          class="flex items-center gap-1.5">
          <span class="w-14 shrink-0 text-gray-500 dark:text-gray-400">
            {{ t(`thinkingToggle.${level}`) }}
          </span>
          <InputText
            :model-value="draft.level_map[level] ?? ''"
            class="min-w-0 flex-1"
            :disabled="draft.level_map[level] === null"
            :placeholder="t('thinkingCustom.valuePlaceholder')"
            :data-test="`thinking-custom-level-${level}`"
            @update:model-value="value => setLevelValue(level, String(value ?? ''))" />
          <label class="flex shrink-0 items-center gap-1 text-gray-400">
            <input
              type="checkbox"
              :checked="draft.level_map[level] === null"
              :data-test="`thinking-custom-unsupported-${level}`"
              @change="toggleUnsupported(level)" />
            <span>{{ t('thinkingCustom.unsupported') }}</span>
          </label>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <Button
          size="small"
          :label="t('thinkingCustom.save')"
          :loading="busy"
          data-test="thinking-custom-save"
          @click="save" />
        <Button
          size="small"
          severity="secondary"
          variant="text"
          :label="t('thinkingCustom.reset')"
          :disabled="busy"
          data-test="thinking-custom-reset"
          @click="reset" />
        <span
          v-if="error"
          class="text-red-500"
          data-test="thinking-custom-error">
          {{ error }}
        </span>
        <span
          v-else-if="justSaved"
          class="text-gray-400"
          data-test="thinking-custom-saved">
          {{ t('thinkingCustom.saved') }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
// The bridge is imported by its stable specifier so tests can vi.mock it (the
// Nuxt auto-import is compile-time and leaves bare symbols unmockable) —
// the same trade the thinking store makes.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  fetchCustomThinkingMapping,
  setCustomThinkingMapping,
  type CustomThinkingMapping,
  type ThinkingLevel
} from '~/composables/bridge/session';
import { THINKING_LEVELS } from '~/stores/thinking';

const props = defineProps<{ sessionId: string }>();
const { t } = useI18n();

/** The seven levels, in clamp order (the rows the form renders). */
const LEVELS: ThinkingLevel[] = THINKING_LEVELS;

/** Wire formats the backend accepts; `custom` writes the dotted field verbatim. */
const FORMAT_OPTIONS = [
  { value: 'custom', label: 'custom' },
  { value: 'openai', label: 'openai' },
  { value: 'deepseek', label: 'deepseek' },
  { value: 'zai', label: 'zai' },
  { value: 'qwen', label: 'qwen' },
  { value: 'together', label: 'together' },
  { value: 'baseten', label: 'baseten' },
  { value: 'openrouter', label: 'openrouter' },
  { value: 'chat-template', label: 'chat-template' },
  { value: 'string-thinking', label: 'string-thinking' },
  { value: 'ant-ling', label: 'ant-ling' }
];

const open = ref(false);
const busy = ref(false);
const error = ref('');
const justSaved = ref(false);
/** Whether the session currently HAS a saved mapping (shows the pill). */
const saved = ref(false);

function blankDraft(): CustomThinkingMapping {
  return { wire_field: '', wire_format: 'custom', level_map: {} };
}

const draft = ref<CustomThinkingMapping>(blankDraft());

/**
 * Load the session's mapping into the form (a saved one opens the section).
 * @param sessionId
 */
async function hydrate(sessionId: string): Promise<void> {
  error.value = '';
  if (!sessionId) return;
  try {
    const { mapping } = await fetchCustomThinkingMapping(sessionId);
    saved.value = mapping !== null;
    draft.value = mapping
      ? {
          wire_field: mapping.wire_field ?? '',
          wire_format: mapping.wire_format ?? 'custom',
          level_map: { ...(mapping.level_map ?? {}) }
        }
      : blankDraft();
    if (mapping) open.value = true;
  } catch {
    // A read failure must not break the enclosing panel: show the blank form.
    draft.value = blankDraft();
  }
}

watch(
  () => props.sessionId,
  sessionId => {
    if (sessionId) hydrate(sessionId);
  },
  { immediate: true }
);

/**
 * Set one level's wire value (an empty string clears the row).
 * @param level
 * @param value
 */
function setLevelValue(level: ThinkingLevel, value: string): void {
  draft.value = {
    ...draft.value,
    level_map: { ...draft.value.level_map, [level]: value || null }
  };
}

/**
 * The "not supported" checkbox: checking records null (the model has no such
 * level), unchecking clears the value — which the server reads the same way
 * until the user types one.
 * @param level
 */
function toggleUnsupported(level: ThinkingLevel): void {
  const current = draft.value.level_map[level];
  setLevelValue(level, current === null ? '' : '');
}

/** Persist the draft (validated server-side; a refusal shows inline). */
async function save(): Promise<void> {
  busy.value = true;
  error.value = '';
  justSaved.value = false;
  try {
    await setCustomThinkingMapping(props.sessionId, draft.value);
    saved.value = true;
    justSaved.value = true;
    setTimeout(() => (justSaved.value = false), 1500);
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : t('thinkingCustom.saveFailed');
  } finally {
    busy.value = false;
  }
}

/** Drop the mapping (back to the declarative defaults). */
async function reset(): Promise<void> {
  busy.value = true;
  error.value = '';
  try {
    await setCustomThinkingMapping(props.sessionId, null);
    draft.value = blankDraft();
    saved.value = false;
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : t('thinkingCustom.saveFailed');
  } finally {
    busy.value = false;
  }
}
</script>

<i18n lang="json">
{
  "zh": {
    "thinkingCustom": {
      "title": "思考等级高级配置",
      "active": "自定义生效中",
      "hint": "为内置格式未覆盖的 provider 手写 wire 字段与各等级取值；保存后下一轮生效。",
      "wireField": "wire 字段路径",
      "wireFieldPlaceholder": "如 extra_body.thinking.type",
      "wireFormat": "wire 格式",
      "levels": "等级映射",
      "unsupported": "不支持",
      "valuePlaceholder": "取值",
      "save": "保存",
      "reset": "恢复默认",
      "saved": "已保存",
      "saveFailed": "保存失败"
    }
  },
  "en": {
    "thinkingCustom": {
      "title": "Advanced thinking config",
      "active": "Custom mapping active",
      "hint": "Hand-write the wire field and per-level values for a provider the built-in formats do not cover; saves apply from the next turn.",
      "wireField": "Wire field path",
      "wireFieldPlaceholder": "e.g. extra_body.thinking.type",
      "wireFormat": "Wire format",
      "levels": "Level mapping",
      "unsupported": "N/A",
      "valuePlaceholder": "value",
      "save": "Save",
      "reset": "Reset to default",
      "saved": "Saved",
      "saveFailed": "Save failed"
    }
  },
  "ja": {
    "thinkingCustom": {
      "title": "思考レベルの詳細設定",
      "active": "カスタム適用中",
      "hint": "組み込みフォーマットが対応しないプロバイダ向けに wire フィールドと各レベルの値を直接指定します（次ターンから有効）。",
      "wireField": "wire フィールドパス",
      "wireFieldPlaceholder": "例: extra_body.thinking.type",
      "wireFormat": "wire フォーマット",
      "levels": "レベルマッピング",
      "unsupported": "非対応",
      "valuePlaceholder": "値",
      "save": "保存",
      "reset": "既定に戻す",
      "saved": "保存しました",
      "saveFailed": "保存に失敗しました"
    }
  },
  "ko": {
    "thinkingCustom": {
      "title": "사고 레벨 고급 설정",
      "active": "사용자 지정 적용 중",
      "hint": "내장 형식이 지원하지 않는 제공자용으로 wire 필드와 레벨별 값을 직접 지정합니다(다음 턴부터 적용).",
      "wireField": "wire 필드 경로",
      "wireFieldPlaceholder": "예: extra_body.thinking.type",
      "wireFormat": "wire 형식",
      "levels": "레벨 매핑",
      "unsupported": "미지원",
      "valuePlaceholder": "값",
      "save": "저장",
      "reset": "기본값 복원",
      "saved": "저장됨",
      "saveFailed": "저장 실패"
    }
  }
}
</i18n>
