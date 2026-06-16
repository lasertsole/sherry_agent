<template>
  <div
    :class="[
      'p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white',
      'dark:bg-[#2a2a36]/[0.6] dark:border-[#555] md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]',
      { 'text-white bg-[#e4efff] dark:bg-[#c1d6e5]': props.isActive }
    ]"
    @click="emits('chooseSession', props.historyRecord.id)">
    <!-- 标题 -->
    <div class="flex gap-1 items-center">
      <Checkbox
        size="small"
        v-model="modelList"
        :value="props.historyRecord.id"
        @click.stop />
      <div class="truncate">{{ props.historyRecord?.title }}</div>
    </div>
    <!-- 创建时间 & 操作 -->
    <div class="flex justify-between mt-3 text-xs">
      <span>创建时间：{{ props.historyRecord?.createTime }}</span>
      <span class="md:hidden pi pi-ellipsis-h"></span>
      <div class="hidden md:flex gap-3">
        <span class="pi pi-trash"></span>
        <span class="pi pi-pen-to-square"></span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 组件

// 方法/类型
import type { SessionRecord } from '../type';

const modelList = defineModel('selectedList', { type: Array, default: () => [] });

interface Props {
  historyRecord: SessionRecord;
  isActive: boolean;
}
const props = defineProps<Props>();

const emits = defineEmits<{
  chooseSession: [id: string];
}>();
</script>
