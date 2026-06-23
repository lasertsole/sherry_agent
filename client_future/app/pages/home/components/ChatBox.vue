<template>
  <div class="flex-1 border-b border-solid border-gray-light dark:border-gray-dark overflow-auto p-3">
    <div
      :class="[
        'flex-1 flex justify-start mt-6 gap-3',
        { 'flex-row-reverse text-right': message.role === CHAT_ROLE.USER },
        { 'text-left': message.role === CHAT_ROLE.AI },
        { hidden: message.role === CHAT_ROLE.TOOL || !message.content }
      ]"
      v-for="message in props.messages"
      :key="message.id">
      <div class="flex justify-center items-center w-10 h-10 bg-[#ddd] rounded-full">
        <span class="pi pi-user"></span>
      </div>
      <div :class="['flex flex-col max-w-[60%]', message.role === CHAT_ROLE.USER ? 'items-end' : 'items-start']">
        <div
          :class="[
            'flex items-center gap-2 mb-1',
            { 'text-right justify-end': message.role === CHAT_ROLE.USER },
            { 'text-left': message.role === CHAT_ROLE.AI }
          ]">
          <span class="text-sm font-semibold text-[#111827] dark:text-[#E5E7EB]">{{
            message.role === CHAT_ROLE.AI ? '橘雪莉' : '我'
          }}</span>
          <span class="text-xs font-normal text-[#6B7280] dark:text-[#9CA3AF]">{{
            formatCompactTimeString(message.timestamp)
          }}</span>
        </div>
        <div
          :class="[
            'w-fit p-3 text-sm font-normal leading-relaxed shadow-sm break-words transition-colors duration-200',
            message.role === CHAT_ROLE.USER
              ? 'bg-[#2563EB] text-[#FFFFFF] rounded-s-xl rounded-ee-xl dark:bg-[#3B82F6]' /* 右侧气泡：蓝色，左下角/右下角圆角定制 */
              : 'bg-white text-gray-900 rounded-e-xl rounded-es-xl border border-gray-100' /* 左侧气泡：白色 */
          ]">
          {{ message.content }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 组件

// 方法/类型
import type { MessageItem } from '../type';
import { CHAT_ROLE } from '../type';
import { formatCompactTimeString } from '@/common/utils';

interface Props {
  messages: MessageItem[] | undefined;
}
const props = withDefaults(defineProps<Props>(), {
  messages: () => [] as MessageItem[]
});
</script>
