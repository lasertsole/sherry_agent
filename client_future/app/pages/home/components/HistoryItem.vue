<template>
  <div
    :class="[
      'p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white',
      'dark:bg-[#2a2a36]/[0.6] dark:border-[#555] hover:bg-[#e4efff] dark:hover:bg-[#c1d6e5]',
      { 'text-white bg-[#e4efff] dark:bg-[#c1d6e5]': props.isActive }
    ]"
    @click="emits('chooseSession', props.HistoryRecord.id)">
    <!-- 标题 -->
    <h1 class="truncate">{{ props.HistoryRecord?.title }}</h1>
    <!-- 创建时间 & 操作 -->
    <div class="flex justify-between mt-3 text-xs">
      <span>创建时间：{{ props.HistoryRecord?.createTime }}</span>
      <span class="md:hidden">设置</span>
      <div class="hidden md:block">
        <span>删除</span> |
        <span>修改</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 组件

// 方法/类型
import type { SessionRecord } from '../type';

interface Props {
  HistoryRecord: SessionRecord;
  isActive: boolean;
}
const props = defineProps<Props>();

const emits = defineEmits<{
  chooseSession: [id: string];
}>();
</script>
