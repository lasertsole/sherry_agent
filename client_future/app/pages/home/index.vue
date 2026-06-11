<template>
  <div class="homepage_container">
    <!-- 左侧-历史记录区域 -->
    <div class="left_side">
      <!-- LOGO区域 -->
      <div class="logo_area">橘雪莉</div>
      <!-- 记录列表 -->
      <div class="history_records">
        <div class="empty_block">暂无会话记录</div>
        <HistoryItem
          v-for="(item, index) in historyList"
          :key="item.id"
          :-history-record="item"
          :is-active="currentSessionId === item.id"
          @choose-session="(id: string) => currentSessionId = id" />
      </div>
      <!-- TODO 批量删除按钮，待接组件库 -->
      <div class="footer_btn">
        <input type="checkbox" />全选
        <div>批量删除会话</div>
      </div>
    </div>
    <!-- 右侧-会话主体区域 -->
    <div class="right_side">
      <!-- 顶部工具栏 -->
      <div class="header">
        <div
          class="header_tool"
          v-for="tool in headerTools"
          :key="tool.event"
          :title="tool.title"
          @click="handleOperate('headerBar', tool.event)">
          {{ tool.toolName }}
        </div>
      </div>
      <div class="main"></div>
      <div class="footer">
        <div class="tools_bar">
          <div
            class="tool"
            v-for="tool in tools"
            :key="tool.event"
            :title="tool.title"
            @click="handleOperate('toolBar', tool.event)">
            {{ tool.toolName }}
          </div>
        </div>
        <div class="input_bar"></div>
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

/** 历史会话 */
const historyList = ref<SessionRecord[]>([]);

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
      case 'logout':
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
</script>

<style lang="scss" scoped>
@use 'sass:math';
@use '@/common.scss' as common;

.homepage_container {
  @include common.fullViewWindow;
  @include common.flexCenter;
  display: flex;
  color: #fff;

  .left_side {
    width: 360px;
    height: 100%;
    background-color: #2a2a36;
    padding: 16px;
    display: flex;
    flex-direction: column;

    .logo_area {
      height: 60px;
      font-size: 20px;
    }

    .history_records {
      display: flex;
      flex-direction: column;
      gap: 12px;
      overflow: auto;
      flex: 1;

      .empty_block {
        @include common.flexCenter;
        height: 100%;
        width: 100%;
        color: #868686;
      }
    }

    .footer_btn {
      border-top: 1px solid #5c5c6c;
      height: 68px;
    }
  }

  .right_side {
    flex: 1;
    height: 100%;
    background-color: #131619;
    display: flex;
    flex-direction: column;

    .header {
      height: 60px;
      padding: 12px;
      box-sizing: border-box;
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid #5c5c6c;

      .header_tool {
        height: 36px;
        line-height: 36px;
        padding: 0 8px;
        border-radius: 4px;
        cursor: pointer;

        &:hover {
          background-color: #363645;
        }
      }
    }

    .main {
      flex: 1;
      border-bottom: 1px solid #5c5c6c;
    }

    .footer {
      height: 160px;
      display: flex;
      flex-direction: column;

      .tools_bar {
        height: 30px;
        padding: 0 12px;
        display: flex;
        align-items: center;
        gap: 12px;
        border-bottom: 1px solid #5c5c6c;

        .tool {
          padding: 0 8px;
          border-radius: 4px;
          cursor: pointer;

          &:hover {
            background-color: #363645;
          }
        }
      }

      .input_bar {
        flex: 1;
      }
    }
  }
}
</style>
