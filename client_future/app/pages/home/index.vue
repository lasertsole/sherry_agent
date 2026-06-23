<template>
  <div class="w-full h-full flex text-theme-main">
    <!-- 移动端菜单遮罩层 -->
    <div
      v-if="isSidebarOpen"
      class="fixed inset-0 bg-[#ddd] opacity-30 z-40 md:hidden"
      @click="isSidebarOpen = false"></div>

    <!-- 左侧-历史记录区域 -->
    <!-- 移动端：固定定位，默认隐藏，通过按钮切换 -->
    <!-- md：固定定位，显示宽度 280px -->
    <!-- lg：相对定位，显示宽度 360px -->
    <div
      :class="[
        'flex flex-col px-4 fixed md:relative h-full md:h-auto md:translate-x-0',
        'transition-transform duration-300 z-50 md:z-auto w-[280px] md:w-[280px] lg:w-[360px]',
        'border-r border-solid border-gray-light bg-[#fff] dark:border-gray-dark dark:bg-[#2a2a36]',
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
          :history-record="item"
          :is-active="currentSessionId === item.id"
          @choose-session="handleToggleSession"
          v-model:selectedList="selectedSessionIds" />
      </div>
      <div class="h-17 flex items-center justify-between">
        <div class="flex items-center justify-center gap-1">
          <Checkbox
            v-model="isCheckAllSession"
            :indeterminate="isIndeterminate"
            binary />
          <span>全选</span>
        </div>
        <Button
          icon="pi pi-trash"
          label="批量删除对话" />
      </div>
    </div>

    <!-- 右侧-会话主体区域 -->
    <div class="flex flex-col flex-1 h-full bg-white dark:bg-[#131619]">
      <!-- 顶部工具栏 -->
      <div
        class="flex md:justify-end justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- 移动端菜单切换按钮 -->
        <Button
          icon="pi pi-bars"
          class="md:hidden"
          variant="text"
          @click="isSidebarOpen = !isSidebarOpen" />
        <!-- 移动端展示 -->
        <div class="md:hidden h-full flex items-center text-xl">🍊橘雪莉</div>
        <!-- 顶部工具栏 -->
        <div class="flex items-center gap-3">
          <ModeSwitch />
          <Button
            icon="pi pi-cog"
            class="md:hidden"
            @click="openHeaderMenu"
            variant="text"
            type="button"
            aria-haspopup="true"
            aria-controls="header_tools" />
          <Menu
            class="md:hidden"
            ref="headerToolsMenuRef"
            id="header_tools"
            :model="headerTools"
            :popup="true"></Menu>
          <div class="hidden md:flex justify-end items-center flex-1 gap-3">
            <Button
              :icon="tool.icon"
              v-for="tool in headerTools"
              :key="tool.event"
              :title="tool.title"
              @click="handleOperate('headerBar', tool.event)"
              :label="tool.toolName"
              variant="text" />
          </div>
        </div>
      </div>
      <!-- 聊天主体 -->
      <ChatBox :messages="currentSession?.messages" />
      <!-- 聊天输入框区域 -->
      <div class="flex flex-col h-40">
        <!-- 聊天工具 -->
        <div class="h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
          <template class="hidden sm:block">
            <Button
              v-for="tool in tools"
              :key="tool.event"
              :icon="tool.icon"
              :label="tool.toolName"
              @click="handleOperate('toolBar', tool.event)"
              size="small"
              variant="text" />
          </template>
          <template class="block sm:hidden">
            <Button
              v-for="tool in tools"
              :key="tool.event"
              :icon="tool.icon"
              :aria-label="tool.toolName"
              @click="handleOperate('toolBar', tool.event)"
              size="small"
              variant="text" />
          </template>
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
import ModeSwitch from './components/ModeSwitch.vue';
import ChatBox from './components/ChatBox.vue';
// function
import { computed } from 'vue';
import type { SessionRecord } from './type.ts';
import { tools, headerTools } from './config';
import { Menu } from 'primevue';

/** 侧边栏展开状态（移动端） */
const isSidebarOpen = ref(false);

/** 历史会话 */
const historyList = ref<SessionRecord[]>([
  {
    id: '1',
    title: '示例会话',
    createTime: '2026-06-17 10:42'
  }
]);

/** 当前会话 */
const currentSession = ref<SessionRecord>();
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

/** 会话切换 */
const handleToggleSession = (id: string) => {
  currentSessionId.value = id;
  isSidebarOpen.value = false;
};

/** 全选状态 */
const isCheckAllSession = ref<boolean>(false);
/** 选择的会话 */
const selectedSessionIds = ref<string[]>([]);
/** 会话选择状态 */
const isIndeterminate = computed(() => {
  if (selectedSessionIds.value.length > 0 && selectedSessionIds.value.length < historyList.value.length) {
    return true;
  } else {
    return false;
  }
});
/** 监听选择 */
watch(
  () => selectedSessionIds.value,
  newVal => {
    if (newVal.length === historyList.value.length) {
      isCheckAllSession.value = true;
    } else {
      isCheckAllSession.value = false;
    }
  }
);

const headerToolsMenuRef = ref<InstanceType<typeof Menu>>();
const openHeaderMenu = (event: Event) => {
  headerToolsMenuRef.value?.toggle(event);
};

get_history_by_page('main', 10, 10, 1);
</script>
