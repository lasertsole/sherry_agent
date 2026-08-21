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
      <div class="truncate">
        {{ props.historyRecord?.title || t('history.newSession') }}
      </div>
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
</script>
