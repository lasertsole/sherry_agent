<template>
  <div
    :class="[
      'p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white',
      'dark:bg-[#2a2a36]/[0.6] dark:border-[#555] md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]',
      { 'text-theme-main bg-[#c1d6e5]! ': props.isActive }
    ]"
    @click="emits('chooseSession', props.historyRecord.id)">
    <!-- 标题 -->
    <div class="flex gap-1 items-center">
      <Checkbox
        size="small"
        v-model="modelList"
        :value="props.historyRecord.id"
        @click.stop />
      <InputText
        v-if="isEditing"
        ref="titleInputRef"
        v-model="draftTitle"
        size="small"
        class="flex-1 min-w-0"
        :placeholder="t('history.titlePlaceholder')"
        :aria-label="t('history.editTitle')"
        @click.stop
        @keydown.enter.stop.prevent="commitRename"
        @keydown.escape.stop.prevent="cancelRename"
        @blur="commitRename" />
      <div
        v-else
        class="truncate"
        :class="props.historyRecord.renamed ? 'text-amber-600 dark:text-amber-400' : ''">
        {{ props.historyRecord?.title || t('history.newSession') }}
      </div>
      <!-- 重命名入口：铅笔图标，点击进入内联编辑 -->
      <span
        v-if="!isEditing"
        class="pi pi-pencil cursor-pointer hover:text-blue-500 shrink-0"
        :title="t('history.editTitle')"
        @click.stop="startEdit"></span>
    </div>
    <!-- 会话 ID（session_id） -->
    <div class="truncate mt-1 text-[10px] text-gray-600 dark:text-gray-400">
      {{ props.historyRecord.id }}
    </div>
    <!-- 创建时间 & 操作 -->
    <div class="flex justify-between mt-3 text-xs">
      <span>{{ t('history.createdAt', { time: formattedCreateTime }) }}</span>
      <span class="pi pi-trash cursor-pointer hover:text-red-500" @click.stop="handleDelete"></span>
    </div>
  </div>
</template>

<script setup lang="ts">
// 方法/类型
import { computed } from 'vue';
import type { SessionRecord } from '../type';
import { useI18n } from 'vue-i18n';
import { formatCompactTimeString } from '@/common/utils';

const { t } = useI18n();

const modelList = defineModel('selectedList', { type: Array, default: () => [] });

interface Props {
  historyRecord: SessionRecord;
  isActive: boolean;
}
const props = defineProps<Props>();

const emits = defineEmits<{
  chooseSession: [id: string];
  deleteSession: [id: string];
  renameSession: [id: string, title: string];
}>();

/** 将后端的紧凑时间串（YYYYMMDDHHmmss）按当前语言的日期格式渲染 */
const formattedCreateTime = computed(() => {
  const timeStr = props.historyRecord?.createTime;
  if (!timeStr) return '';
  const format = t('history.dateFormat') || 'YYYY-MM-DD HH:mm';
  return formatCompactTimeString(timeStr, format);
});

/** 删除会话：确认后向父组件发出 deleteSession 事件 */
const handleDelete = () => {
  if (window.confirm(t('history.deleteConfirm'))) {
    emits('deleteSession', props.historyRecord.id);
  }
};

/** 内联编辑状态 */
const isEditing = ref(false);
const draftTitle = ref('');
const titleInputRef = ref<{ $el: HTMLInputElement } | null>(null);

/** 进入编辑：预填当前标题（空则空串），聚焦并全选便于直接替换 */
async function startEdit() {
  draftTitle.value = props.historyRecord.title || '';
  isEditing.value = true;
  await nextTick();
  titleInputRef.value?.$el?.focus();
  titleInputRef.value?.$el?.select();
}

/** 提交：守卫先行防止 Enter 后失焦双提交；空标题视为取消；未变化不触发 */
function commitRename() {
  if (!isEditing.value) return;
  isEditing.value = false;
  const next = draftTitle.value.trim();
  const original = props.historyRecord.title || '';
  if (next && next !== original) emits('renameSession', props.historyRecord.id, next);
}

/** 取消编辑（Esc）：同样守卫，避免 Enter 提交后 Esc 误回滚 */
function cancelRename() {
  if (!isEditing.value) return;
  isEditing.value = false;
}
</script>
