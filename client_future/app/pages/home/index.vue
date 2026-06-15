<template>
  <div class="w-full h-full flex text-theme-main">
    <!-- 移动端菜单遮罩层 -->
    <div
      v-if="isSidebarOpen"
      class="fixed inset-0 bg-black/50 z-40 md:hidden"
      @click="isSidebarOpen = false"></div>

    <!-- 左侧-历史记录区域 -->
    <!-- 移动端：固定定位，默认隐藏，通过按钮切换 -->
    <!-- md：固定定位，显示宽度 280px -->
    <!-- lg：相对定位，显示宽度 360px -->
    <div
      :class="[
        'flex flex-col px-4 fixed md:relative h-full md:h-auto md:translate-x-0',
        'transition-transform duration-300 z-50 md:z-auto w-[280px] md:w-[280px] lg:w-[360px]',
        'border-r border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#2a2a36]',
        isSidebarOpen ? 'translate-x-0' : '-translate-x-full'
      ]">
      <!-- LOGO区域 -->
      <div class="flex items-center h-15 text-xl">🍊橘雪莉</div>
      <!-- 记录列表 -->
      <div class="flex flex-col overflow-auto flex-1 gap-3">
        <div
          v-if="historyList.length === 0"
          class="flex items-center justify-center h-full w-full text-[#868686]">
          暂无会话记录
        </div>
        <HistoryItem
          v-for="(item, index) in historyList"
          :key="item.id"
          :-history-record="item"
          :is-active="currentSessionId === item.id"
          @choose-session="handleToggleSession" />
      </div>
      <!-- TODO 批量删除按钮，待接组件库 -->
      <div class="h-17 flex items-center justify-between">
        <div class="flex items-center justify-center gap-1"><input type="checkbox" />全选</div>
        <div>批量删除会话</div>
      </div>
    </div>

    <!-- 右侧-会话主体区域 -->
    <div class="flex flex-col flex-1 h-full bg-white dark:bg-[#131619]">
      <!-- 顶部工具栏 -->
      <div
        class="flex justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- 移动端菜单切换按钮 -->
        <button
          class="md:hidden aspect-square h-full flex items-center justify-center text-theme-main rounded-lg bg-[#2a2a36]"
          @click="isSidebarOpen = !isSidebarOpen">
          <span v-if="!isSidebarOpen">☰</span>
          <span v-else>✕</span>
        </button>
        <!-- 移动端展示 -->
        <div class="md:hidden h-full flex items-center text-xl">🍊橘雪莉</div>
        <!-- 顶部工具栏 -->
        <button
          class="sm:hidden aspect-square h-full flex items-center justify-center text-theme-main rounded-lg bg-[#2a2a36]"
          @click="isToolsMenuOpen = !isToolsMenuOpen">
          <span>设置</span>
        </button>
        <div class="hidden sm:flex justify-end items-center flex-1 gap-3">
          <div
            class="cursor-pointer h-9 px-2 leading-9 rounded-lg bg-gray-light dark:bg-gray-dark"
            v-for="tool in headerTools"
            :key="tool.event"
            :title="tool.title"
            @click="handleOperate('headerBar', tool.event)">
            {{ tool.toolName }}
          </div>
        </div>
      </div>
      <!-- 聊天主体 -->
      <div class="flex-1 border-b border-solid border-gray-light dark:border-gray-dark"></div>
      <!-- 聊天输入框区域 -->
      <div class="flex flex-col h-40">
        <!-- 聊天工具 -->
        <div class="h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
          <div
            class="cursor-pointer px-2 rounded bg-gray-light dark:bg-gray-dark"
            v-for="tool in tools"
            :key="tool.event"
            :title="tool.title"
            @click="handleOperate('toolBar', tool.event)">
            {{ tool.toolName }}
          </div>
        </div>
        <!-- 输入框 -->
        <div class="flex-1"></div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
// components
import HistoryItem from './components/HistoryItem.vue';
// function
import type { SessionRecord } from './type.ts';
import { tools, headerTools } from './config';

/** 侧边栏展开状态（移动端） */
const isSidebarOpen = ref(false);

/** 工具栏展开状态（移动端） */
const isToolsMenuOpen = ref(false);

/** 颜色主题 */
const colorMode = useColorMode();

/** 历史会话 */
const historyList = ref<SessionRecord[]>([
  {
    title: '这是历史吼吼吼吼吼吼吼吼吼吼吼吼吼吼吼吼水水水水水水水水水水',
    id: '1',
    createTime: '2026-06-12 11:04'
  }
]);

/** 当前会话 */
const currentSessionId = ref<string>();

/** 工具触发 */
const handleOperate = (type: string, event: string) => {
  if (!event || !type) return;
  // 头部区域
  if (type === 'headerBar') {
    switch (event) {
      case 'userCenter':
        return;
      case 'knowledgeBase':
        return;
      case 'changeTheme':
        handleToggleTheme();
        return;
      default:
        return;
    }
  } else {
    // 工具栏
    switch (event) {
      case 'createSession':
        handleCreateSession();
        return;
      case 'knowledgeBase':
        return;
      case 'uploadFile':
        return;
      case 'uploadImage':
        return;
      default:
        return;
    }
  }
};

/** 新增会话 */
const handleCreateSession = () => {};

/** 主题切换 */
const handleToggleTheme = () => {
  console.log(colorMode.preference);
  if (colorMode.preference === 'dark') {
    colorMode.preference = 'light';
  } else if (colorMode.preference === 'light') {
    colorMode.preference = 'dark';
  } else {
    colorMode.preference = 'dark';
  }
};

/** 会话切换 */
const handleToggleSession = (id: string) => {
  currentSessionId.value = id;
  isSidebarOpen.value = false;
};

get_history_turn_message("main", 10)
</script>
