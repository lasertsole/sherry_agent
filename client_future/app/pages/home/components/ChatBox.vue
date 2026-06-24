<template>
  <div class="flex-1 border-b border-solid border-gray-light dark:border-gray-dark overflow-auto p-3">
    <div
      v-for="(message, index) in filteredMessages"
      :key="message.id"
      :class="[
        'flex-1 flex justify-start gap-3',
        { 'flex-row-reverse text-right': message.role === CHAT_ROLE.USER },
        { 'text-left': message.role === CHAT_ROLE.AI },
        { hidden: message.role === CHAT_ROLE.TOOL },
        filteredMessages?.[index - 1]?.role === message.role ? 'mt-1' : 'mt-6'
      ]">
      <div class="flex justify-center items-center w-10 h-10 rounded-full">
        <!-- 头像区域，连续消息不展示头像 -->
        <span :class="['pi pi-user', { hidden: filteredMessages?.[index - 1]?.role === message.role }]"></span>
      </div>
      <!-- 消息主体 -->
      <div :class="['flex flex-col max-w-[60%]', message.role === CHAT_ROLE.USER ? 'items-end' : 'items-start']">
        <!-- 用户 时间 -->
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
        <!-- 内容 -->
        <div
          :class="[
            'w-fit p-3 text-sm font-normal leading-relaxed shadow-sm break-words transition-colors duration-200',
            message.role === CHAT_ROLE.USER
              ? 'bg-[#2563EB] text-[#FFFFFF] rounded-s-xl rounded-ee-xl dark:bg-[#3B82F6]' /* 右侧气泡：蓝色，左下角/右下角圆角定制 */
              : 'bg-white text-gray-900 rounded-e-xl rounded-es-xl border border-gray-100' /* 左侧气泡：白色 */,
            { 'rounded-xl': filteredMessages?.[index - 1]?.role === message.role }
          ]">
          <div v-html="safeHtml(message.content)"></div>
          <template v-if="message?.tool_calls?.length">
            <div
              v-for="tool in message.tool_calls"
              :key="tool.id"
              class="font-serif text-slate-500 font-bold">
              🛠️正在调用工具{{ tool.name }}...
            </div>
          </template>
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
import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';

interface Props {
  messages: MessageItem[] | undefined;
}
const props = withDefaults(defineProps<Props>(), {
  messages: () => [] as MessageItem[]
});

/** 过滤tool后的消息列表 */
const filteredMessages = computed(() => {
  return props.messages.filter(item => item.role !== CHAT_ROLE.TOOL);
});

// 初始化 markdown-it
const md = new MarkdownIt({ html: true, linkify: true });

/** 解析 MD 并进行 XSS 净化 */
const safeHtml = computed(() => (content: string) => {
  // 先把 markdown 转为原始 html 字符串
  const rawHtml = md.render(content);

  // 使用 DOMPurify 清理所有危险的标签（如 script）和属性（如 onerror）
  return DOMPurify.sanitize(rawHtml, {
    // 选填配置：如果你希望点击链接在新窗口打开，可以保留 target="_blank"
    ADD_ATTR: ['target']
  });
});
</script>
