<template>
  <div
    class="todo-item flex items-center gap-2 px-3 py-1"
    :class="{ 'opacity-60': isTerminal }">
    <i
      :class="['text-xs shrink-0', statusIcon]"
      aria-hidden="true" />
    <span
      v-if="showCategory"
      class="text-[10px] leading-none px-1.5 py-0.5 rounded bg-surface-200 dark:bg-surface-700 text-color-secondary shrink-0"
      >{{ todo.category }}</span
    >
    <span
      :class="['text-sm flex-1 truncate', isTerminal ? 'line-through text-color-secondary' : 'text-color']"
      :title="linkedLabel"
      v-tooltip.top="linkedLabel"
      >{{ todo.content }}</span
    >
    <span
      v-if="todo.status === 'in_progress'"
      class="pulse-dot w-2 h-2 rounded-full bg-primary inline-block shrink-0" />
    <i
      v-if="todo.delegation === 'subagent'"
      class="pi pi-external-link text-xs text-color-secondary shrink-0"
      v-tooltip.top="t('todolist.delegated')" />
  </div>
</template>

<script lang="ts" setup>
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { Todo } from '~/composables/use-todo-list';

const props = defineProps<{ todo: Todo }>();
const { t } = useI18n();

const isTerminal = computed(() => props.todo.status === 'completed' || props.todo.status === 'cancelled');

const showCategory = computed(() => !!props.todo.category && props.todo.category !== 'quick');

const statusIcon = computed(() => {
  switch (props.todo.status) {
    case 'completed':
      return 'pi pi-check-circle text-green-500';
    case 'cancelled':
      return 'pi pi-times-circle text-color-secondary';
    case 'in_progress':
      return 'pi pi-spin pi-spinner text-primary';
    default:
      return 'pi pi-circle text-color-secondary';
  }
});

const linkedLabel = computed(() => {
  const flow = props.todo.flow_id;
  const step = props.todo.step_id;
  if (!flow) return '';
  return step ? t('todolist.linkedStep', { flow, step }) : t('todolist.flowLabel', { flow });
});
</script>
