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
      <div class="flex justify-center items-center w-10 h-10 rounded-full overflow-hidden shrink-0 bg-gray-100 dark:bg-gray-800">
        <!-- 头像区域，连续消息不展示头像 -->
        <img
          v-if="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
          :class="['w-full h-full object-cover', { hidden: filteredMessages?.[index - 1]?.role === message.role }]"
          :src="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
          :alt="message.role === CHAT_ROLE.USER ? userName : aiName" />
        <span
          v-else
          :class="['pi pi-user', { hidden: filteredMessages?.[index - 1]?.role === message.role }]"></span>
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
            message.role === CHAT_ROLE.AI ? aiName : userName
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
  /** 用户头像 URL（服务端返回） */
  userAvatar?: string;
  /** AI 头像 URL（服务端返回） */
  aiAvatar?: string;
  /** 用户显示名（服务端返回） */
  userName?: string;
  /** AI 显示名（服务端返回） */
  aiName?: string;
}
const props = withDefaults(defineProps<Props>(), {
  messages: () => [] as MessageItem[],
  userAvatar: '',
  aiAvatar: '',
  userName: '我',
  aiName: '橘雪莉'
});

/** 过滤tool后的消息列表 */
const isToolCallMsg = (msg: MessageItem) =>
  (msg as unknown as { tool_calls?: unknown[] }).tool_calls?.length;
const filteredMessages = computed(() => {
  return props.messages.filter((item: MessageItem) => {
    // 隐藏 tool 消息
    if (item.role === CHAT_ROLE.TOOL) {
      return false;
    }
    // 隐藏「AI 空占位」消息：发送后 AI 尚未产出任何内容（也无工具调用）时，
    // 不渲染这个只有名字+空白框的占位气泡，避免「橘雪莉」看起来贴在白框里。
    if (item.role === CHAT_ROLE.AI && !item.content.trim() && !isToolCallMsg(item)) {
      return false;
    }
    return true;
  });
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
