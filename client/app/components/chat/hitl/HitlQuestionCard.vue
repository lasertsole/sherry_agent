<template>
  <!-- Batch mode: 1-4 questions, each with its own options,
       custom answer and annotation box, submitted together. -->
  <template v-if="isMultiQuestion">
    <div class="text-sm font-semibold">{{ t('hitl.title') }}</div>
    <div class="text-xs text-gray-400">{{ t('hitl.multiHint') }}</div>
    <div
      v-for="(q, qi) in questionList"
      :key="qi"
      class="flex flex-col gap-1.5 rounded-lg border border-solid border-gray-light p-2 dark:border-gray-dark"
      :data-test="`hitl-question-${qi}`">
      <div class="text-sm font-medium">{{ q.header || q.question }}</div>
      <div
        v-if="q.header && q.question"
        class="text-xs whitespace-pre-wrap text-gray-500 dark:text-gray-400">
        {{ q.question }}
      </div>
      <div class="flex flex-col gap-1.5">
        <button
          v-for="(option, oi) in q.options"
          :key="oi"
          type="button"
          class="text-left px-3 py-1.5 rounded-lg border border-solid transition-colors cursor-pointer"
          :class="
            isMultiOptionSelected(qi, option.label)
              ? 'border-theme-main bg-theme-main/10'
              : 'border-gray-light dark:border-gray-dark hover:bg-gray-50 dark:hover:bg-[#1a1d21]'
          "
          :data-test="`hitl-question-${qi}-option-${oi}`"
          @click="selectMulti(qi, option.label)">
          <span class="text-sm font-medium">{{ option.label }}</span>
          <span
            v-if="option.description"
            class="text-xs text-gray-400 ml-2">
            {{ option.description }}
          </span>
          <pre
            v-if="option.preview"
            class="mt-1 m-0 overflow-x-auto rounded bg-gray-50 p-2 text-[11px] dark:bg-gray-800"
            >{{ option.preview }}</pre>
        </button>
        <input
          v-model="multiCustomAnswers[qi]"
          type="text"
          class="px-3 py-1.5 text-sm rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-transparent outline-none focus:border-theme-main"
          :placeholder="t('hitl.customPlaceholder')"
          :data-test="`hitl-question-${qi}-custom`" />
        <textarea
          v-if="q.allowAnnotation"
          v-model="multiAnnotations[qi]"
          rows="2"
          class="px-3 py-1.5 text-sm rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-transparent outline-none resize-y focus:border-theme-main"
          :placeholder="t('hitl.annotationPlaceholder')"
          :data-test="`hitl-question-${qi}-annotation`" />
      </div>
    </div>
    <div class="flex items-center justify-end gap-2">
      <Button
        :label="t('hitl.reject')"
        size="small"
        severity="danger"
        variant="text"
        data-test="hitl-multi-reject"
        @click="rejectQuestions" />
      <Button
        :label="t('hitl.multiSubmit')"
        size="small"
        data-test="hitl-multi-submit"
        @click="submitMultiQuestions" />
    </div>
  </template>
  <!-- Single question (legacy shape, also a 1-item batch): options
       plus a trailing custom-input row. -->
  <template v-else>
    <div class="text-sm font-semibold">{{ questionHeader || t('hitl.title') }}</div>
    <div
      v-if="questionText"
      class="text-sm whitespace-pre-wrap text-gray-700 dark:text-gray-200">
      {{ questionText }}
    </div>
    <div class="flex flex-col gap-1.5">
      <button
        v-for="(option, idx) in questionOptions"
        :key="idx"
        type="button"
        class="text-left px-3 py-1.5 rounded-lg border border-solid transition-colors cursor-pointer"
        :class="
          isMultipleQuestion && selectedQuestionOptions.includes(option.label)
            ? 'border-theme-main bg-theme-main/10'
            : 'border-gray-light dark:border-gray-dark hover:bg-gray-50 dark:hover:bg-[#1a1d21]'
        "
        @click="selectQuestionOption(option.label)">
        <span class="text-sm font-medium">{{ option.label }}</span>
        <span
          v-if="option.description"
          class="text-xs text-gray-400 ml-2">
          {{ option.description }}
        </span>
        <pre
          v-if="option.preview"
          class="mt-1 m-0 overflow-x-auto rounded bg-gray-50 p-2 text-[11px] dark:bg-gray-800"
          >{{ option.preview }}</pre>
      </button>
      <!-- trailing custom-input row -->
      <div class="flex items-center gap-2">
        <input
          v-model="customAnswer"
          type="text"
          class="flex-1 px-3 py-1.5 text-sm rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-transparent outline-none focus:border-theme-main"
          :placeholder="t('hitl.customPlaceholder')"
          @keydown.enter.prevent="submitCustomAnswer" />
        <Button
          :label="t('hitl.submit')"
          size="small"
          :disabled="!customAnswer.trim() && selectedQuestionOptions.length === 0"
          @click="submitCustomAnswer" />
        <Button
          :label="t('hitl.reject')"
          size="small"
          severity="danger"
          variant="text"
          @click="emit('decide', 'reject')" />
      </div>
    </div>
  </template>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { HitlRequestData } from '~/pages/home/type';
import type { HitlDecision } from '~/composables/use-hitl-approval';

/**
 * The `question` tool's approval card: one structured question, or a batch of
 * them. Owns the option/batch form state; every submit goes back through the
 * page's HITL resume as an `approve` decision carrying the answer text, and the
 * reject button sends a plain `reject`.
 */
const props = defineProps<{
  /** The pending approval request (`null` while no card is pending). */
  request: HitlRequestData | null;
}>();

const emit = defineEmits<{
  (e: 'decide', decision: HitlDecision, message?: string): void;
}>();

const { t } = useI18n();

interface QuestionOptionView {
  label: string;
  description?: string;
  /** Optional code/mockup shown under the option for side-by-side comparison. */
  preview?: string;
}
interface QuestionView {
  question: string;
  header: string;
  options: QuestionOptionView[];
  multiple: boolean;
  allowAnnotation: boolean;
}

const questionArgs = computed<Record<string, unknown>>(() => props.request?.tool_args ?? {});
/** True for a batch (2+ questions); older pending interrupts lack the flag. */
const isMultiQuestion = computed(() => questionArgs.value['_multi'] === true);
const questionText = computed(() => {
  const q = questionArgs.value['question'];
  return typeof q === 'string' ? q : (props.request?.description ?? '');
});
const questionHeader = computed(() => {
  const h = questionArgs.value['header'];
  return typeof h === 'string' ? h : '';
});
/**
 * Map one raw option object (batch and flat shapes share it).
 * @param raw
 */
const toQuestionOption = (raw: unknown): QuestionOptionView | null => {
  if (typeof raw !== 'object' || raw === null) return null;
  const o = raw as Record<string, unknown>;
  const label = String(o['label'] ?? '');
  if (!label) return null;
  return {
    label,
    description: typeof o['description'] === 'string' ? o['description'] : undefined,
    preview: typeof o['preview'] === 'string' && o['preview'] ? o['preview'] : undefined
  };
};
const questionOptions = computed<QuestionOptionView[]>(() => {
  const raw = questionArgs.value['options'];
  if (!Array.isArray(raw)) return [];
  return raw.map(toQuestionOption).filter((o): o is QuestionOptionView => o !== null);
});
/** The batch's questions, mapped defensively (an old payload has none). */
const questionList = computed<QuestionView[]>(() => {
  const raw = questionArgs.value['questions'];
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((q): q is Record<string, unknown> => typeof q === 'object' && q !== null)
    .map(q => ({
      question: typeof q['question'] === 'string' ? q['question'] : '',
      header: typeof q['header'] === 'string' ? q['header'] : '',
      options: Array.isArray(q['options'])
        ? (q['options'] as unknown[]).map(toQuestionOption).filter((o): o is QuestionOptionView => o !== null)
        : [],
      multiple: q['multiple'] === true,
      allowAnnotation: q['allow_annotation'] !== false
    }));
});
const isMultipleQuestion = computed(() => questionArgs.value['multiple'] === true);
const customAnswer = ref('');
const selectedQuestionOptions = ref<string[]>([]);
/** Batch state, keyed by question index. */
const multiSelections = ref<Record<number, string[]>>({});
const multiCustomAnswers = ref<Record<number, string>>({});
const multiAnnotations = ref<Record<number, string>>({});

/**
 * Send the chosen answer (option label(s) and/or custom text) back through the HITL resume.
 * @param answer
 */
const submitQuestionAnswer = (answer: string) => {
  customAnswer.value = '';
  selectedQuestionOptions.value = [];
  emit('decide', 'approve', answer);
};

/**
 * Single-select sends immediately; multi-select toggles the option in the selection.
 * @param label
 */
const selectQuestionOption = (label: string) => {
  if (isMultipleQuestion.value) {
    const i = selectedQuestionOptions.value.indexOf(label);
    if (i >= 0) selectedQuestionOptions.value.splice(i, 1);
    else selectedQuestionOptions.value.push(label);
  } else {
    submitQuestionAnswer(label);
  }
};

/** Submit the custom answer, combined with any selected options in multi-select mode. */
const submitCustomAnswer = () => {
  const parts = isMultipleQuestion.value ? [...selectedQuestionOptions.value] : [];
  const text = customAnswer.value.trim();
  if (text) parts.push(text);
  if (parts.length) submitQuestionAnswer(parts.join(', '));
};

/**
 * Select an option on one batch question (single-select replaces, multi toggles).
 * @param qi Question index.
 * @param label Option label.
 */
const selectMulti = (qi: number, label: string) => {
  const q = questionList.value[qi];
  if (!q) return;
  if (!q.multiple) {
    multiSelections.value = { ...multiSelections.value, [qi]: [label] };
    return;
  }
  const current = multiSelections.value[qi] ?? [];
  multiSelections.value = {
    ...multiSelections.value,
    [qi]: current.includes(label) ? current.filter(item => item !== label) : [...current, label]
  };
};

/**
 * Whether an option is selected on one batch question.
 * @param qi Question index.
 * @param label Option label.
 */
const isMultiOptionSelected = (qi: number, label: string): boolean => (multiSelections.value[qi] ?? []).includes(label);

/** Clear every batch draft (a new request or a submitted one starts clean). */
const resetMultiQuestionState = () => {
  multiSelections.value = {};
  multiCustomAnswers.value = {};
  multiAnnotations.value = {};
};

/**
 * Submit every batch question at once: the JSON payload carries each selection
 * (plus its custom text) and the optional annotation.
 */
const submitMultiQuestions = () => {
  const answers = questionList.value.map((q, qi) => {
    const selection = [...(multiSelections.value[qi] ?? [])];
    const custom = (multiCustomAnswers.value[qi] ?? '').trim();
    if (custom) selection.push(custom);
    return {
      question: q.question,
      header: q.header,
      selection,
      annotation: (multiAnnotations.value[qi] ?? '').trim()
    };
  });
  resetMultiQuestionState();
  emit('decide', 'approve', JSON.stringify({ answers }));
};

/** Decline the whole batch (the tool reports the standard decline). */
const rejectQuestions = () => {
  resetMultiQuestionState();
  emit('decide', 'reject');
};

// Reset the question-view state whenever a new HITL request arrives.
watch(
  () => props.request,
  () => {
    customAnswer.value = '';
    selectedQuestionOptions.value = [];
    resetMultiQuestionState();
  }
);
</script>
