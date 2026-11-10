<template>
  <!-- The approval slot shares the composer's height with the input box (the
       page shows one and hides the other). It stays MOUNTED and is only hidden
       while no request pends, so each variant keeps its own form state. -->
  <div
    v-show="!!request"
    class="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col gap-2">
    <HitlQuestionCard
      v-if="isQuestionHitl"
      :request="request"
      @decide="forwardDecision" />
    <HitlPlanCard
      v-else-if="isPlanHitl"
      :request="request"
      @decide="forwardDecision" />
    <HitlGenericCard
      v-else
      :request="request"
      @decide="forwardDecision" />
  </div>
</template>

<script lang="ts" setup>
import { computed } from 'vue';
import type { HitlRequestData } from '~/pages/home/type';
import type { HitlDecision } from '~/composables/use-hitl-approval';
import HitlQuestionCard from './HitlQuestionCard.vue';
import HitlPlanCard from './HitlPlanCard.vue';
import HitlGenericCard from './HitlGenericCard.vue';

/**
 * The pending-approval slot of the composer: picks the question / plan /
 * generic card for the request and forwards its decision to the page's HITL
 * resume. The cards are presentational + form-local; the decision flow stays in
 * `useHitlApproval`.
 */
const props = defineProps<{
  /** The pending approval request (`null` while no card is pending). */
  request: HitlRequestData | null;
}>();

const emit = defineEmits<{
  (e: 'decide', decision: HitlDecision, message?: string): void;
}>();

/** Question tool: one structured question, or a batch of them. */
const isQuestionHitl = computed(() => props.request?.tool_name === 'question');
/** exit_plan_mode: the plan for approval (approve / request changes). */
const isPlanHitl = computed(() => props.request?.tool_name === 'exit_plan_mode');

/**
 * Forward one card decision to the page's HITL resume.
 * @param decision The decision the card chose.
 * @param message Optional resume message (answer text / plan feedback).
 */
function forwardDecision(decision: HitlDecision, message?: string): void {
  emit('decide', decision, message);
}
</script>
