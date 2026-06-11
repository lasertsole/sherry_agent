<template>
  <div
    :class="['item_card', { active: isActive }]"
    @click="emits('chooseSession', props.HistoryRecord.id)">
    <!-- 标题 -->
    <h1 class="session_title">{{ props.HistoryRecord?.title }}</h1>
    <!-- 创建时间 & 操作 -->
    <div class="create_time">
      <span>创建时间：{{ props.HistoryRecord?.createTime }}</span>
      <div>
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

<style scoped lang="scss">
@use '@/common.scss' as common;

.item_card {
  padding: 12px;
  background-color: rgba($color: #2a2a36, $alpha: 0.6);
  border: 1px solid #555;
  border-radius: 10px;
  cursor: pointer;
  color: #ccc;

  &:hover,
  &.active {
    border-color: #777;
    box-shadow: 0 0 10px rgba($color: #777, $alpha: 0.5);
    color: #fff;
  }

  .create_time {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    margin-top: 12px;
  }

  .session_title {
    @include common.wordEllipsis;
  }
}
</style>
