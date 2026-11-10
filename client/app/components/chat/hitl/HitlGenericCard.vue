<template>
  <div class="text-sm font-semibold">{{ t('hitl.title') }}</div>
  <div class="text-sm text-gray-500">
    {{ t('hitl.tool') }}: <span class="font-bold">{{ request?.tool_name }}</span>
  </div>
  <div
    v-if="request?.description"
    class="text-sm whitespace-pre-wrap">
    {{ request.description }}
  </div>
  <div
    v-if="request?.tool_args && Object.keys(request.tool_args).length > 0"
    class="text-xs bg-gray-50 dark:bg-gray-800 p-2 rounded-lg overflow-auto max-h-24">
    <pre class="m-0">{{ JSON.stringify(request.tool_args, null, 2) }}</pre>
  </div>
  <div class="mt-auto flex gap-2 justify-end">
    <Button
      :label="t('hitl.reject')"
      icon="pi pi-times"
      severity="danger"
      size="small"
      @click="emit('decide', 'reject')" />
    <Button
      :label="t('hitl.yolo')"
      icon="pi pi-bolt"
      severity="warn"
      size="small"
      :title="t('hitl.yoloTooltip')"
      @click="emit('decide', 'yolo')" />
    <Button
      v-if="(request?.allowed_decisions ?? []).includes('approve_dir')"
      :label="t('hitl.approveDir')"
      icon="pi pi-folder"
      severity="secondary"
      size="small"
      :title="t('hitl.approveDirTooltip')"
      @click="emit('decide', 'approve_dir')" />
    <Button
      v-if="(request?.allowed_decisions ?? []).includes('session')"
      :label="t('hitl.approveSession')"
      icon="pi pi-bookmark"
      severity="secondary"
      size="small"
      :title="t('hitl.approveSessionTooltip')"
      @click="emit('decide', 'session')" />
    <Button
      :label="t('hitl.approve')"
      icon="pi pi-check"
      size="small"
      @click="emit('decide', 'approve')" />
  </div>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';
import type { HitlRequestData } from '~/pages/home/type';
import type { HitlDecision } from '~/composables/use-hitl-approval';

/**
 * The generic approval card for every tool that is not `question` or
 * `exit_plan_mode`: tool name, description, arguments and the decision buttons
 * the request allows.
 */
defineProps<{
  /** The pending approval request (`null` while no card is pending). */
  request: HitlRequestData | null;
}>();

const emit = defineEmits<{
  (e: 'decide', decision: HitlDecision, message?: string): void;
}>();

const { t } = useI18n();
</script>
