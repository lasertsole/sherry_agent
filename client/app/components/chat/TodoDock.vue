<template>
  <Transition name="dock-slide">
    <div
      v-if="dockVisible"
      class="todo-dock shrink-0 mx-2 mt-2 rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#131619] shadow-sm overflow-hidden">
      <div class="dock-header flex items-center justify-between gap-2 px-3 py-2 select-none">
        <div class="flex items-center gap-2 min-w-0">
          <i
            class="pi pi-list text-xs text-theme-main shrink-0"
            aria-hidden="true" />
          <span class="text-sm font-medium truncate">{{ t('todolist.title') }}</span>
          <span class="text-xs text-color-secondary shrink-0">{{
            t('todolist.progress', { done: doneCount, total: todos.length })
          }}</span>
        </div>
        <Button
          text
          rounded
          size="small"
          :aria-expanded="!collapsed"
          :aria-label="collapsed ? t('todolist.expand') : t('todolist.collapse')"
          @click="toggleCollapsed">
          <i :class="['pi', collapsed ? 'pi-chevron-down' : 'pi-chevron-up']" />
        </Button>
      </div>
      <div
        v-show="!collapsed"
        class="todo-list overflow-y-auto max-h-42 pb-1">
        <div
          v-for="group in groups"
          :key="group.flowId ?? '__unlinked__'"
          class="flow-group">
          <div
            v-if="group.flowId"
            class="flow-label text-[11px] text-color-secondary px-3 py-0.5">
            {{ t('todolist.flowLabel', { flow: group.flowId }) }}
          </div>
          <TodoItem
            v-for="(todo, i) in group.items"
            :key="`${todo.flow_id ?? ''}:${todo.step_id ?? ''}:${i}`"
            :todo="todo" />
        </div>
      </div>
    </div>
  </Transition>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';
import TodoItem from './TodoItem.vue';

const { t } = useI18n();
const { todos, groups, dockVisible, doneCount, collapsed, toggleCollapsed } = useTodoList();
</script>
