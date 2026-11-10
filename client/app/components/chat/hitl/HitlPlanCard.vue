<template>
  <div class="text-sm font-semibold">
    {{ t('hitl.planTitle') }}
    <span
      v-if="planRevision"
      class="text-xs text-gray-400 ml-2">
      {{ planRevision }}
    </span>
  </div>
  <pre
    class="flex-1 min-h-0 overflow-auto text-xs whitespace-pre-wrap bg-gray-50 dark:bg-gray-800 p-2 rounded-lg m-0"
    >{{ planText }}</pre>
  <div class="flex items-center gap-2">
    <input
      v-model="planFeedback"
      type="text"
      class="flex-1 px-3 py-1.5 text-sm rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-transparent outline-none focus:border-theme-main"
      :placeholder="t('hitl.planFeedbackPlaceholder')"
      @keydown.enter.prevent="revisePlan" />
    <Button
      :label="t('hitl.planRevise')"
      size="small"
      severity="warn"
      variant="text"
      :disabled="!planFeedback.trim()"
      @click="revisePlan" />
    <Button
      :label="t('hitl.planApprove')"
      icon="pi pi-check"
      size="small"
      @click="approvePlan" />
  </div>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { HitlRequestData } from '~/pages/home/type';
import type { HitlDecision } from '~/composables/use-hitl-approval';

/**
 * The `exit_plan_mode` approval card: the proposed plan (approve / request
 * changes). A revise sends the typed feedback as the rejection message.
 */
const props = defineProps<{
  /** The pending approval request (`null` while no card is pending). */
  request: HitlRequestData | null;
}>();

const emit = defineEmits<{
  (e: 'decide', decision: HitlDecision, message?: string): void;
}>();

const { t } = useI18n();

const planArgs = computed<Record<string, unknown>>(() => props.request?.tool_args ?? {});
const planText = computed(() => {
  const p = planArgs.value['plan'];
  return typeof p === 'string' ? p : (props.request?.description ?? '');
});
const planRevision = computed(() => {
  const r = planArgs.value['revision'];
  return typeof r === 'string' ? r : '';
});
const planFeedback = ref('');

/** Approve the plan: plan mode exits server-side and execution begins. */
const approvePlan = () => {
  planFeedback.value = '';
  emit('decide', 'approve');
};

/** Send the plan back with the typed feedback (reject + message = revise it). */
const revisePlan = () => {
  const message = planFeedback.value.trim();
  if (!message) return;
  planFeedback.value = '';
  emit('decide', 'reject', message);
};

// A new request starts from a clean feedback box.
watch(
  () => props.request,
  () => {
    planFeedback.value = '';
  }
);
</script>
