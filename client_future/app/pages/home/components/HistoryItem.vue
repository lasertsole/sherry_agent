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
      <span></span>
      <span
        class="pi pi-ellipsis-h md:hidden"
        aria-haspopup="true"
        aria-controls="header_tools"
        @click.stop="openHeaderMenu"></span>
      <Menu
        class="md:hidden"
        ref="mainenuRef"
        :id="`session_item_${props.historyRecord.id}`"
        :model="menuItems"
        :popup="true"></Menu>
      <div class="hidden md:flex gap-3">
        <span class="pi pi-trash"></span>
        <span class="pi pi-pen-to-square"></span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 组件
import { Menu } from 'primevue';
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

interface MenuItem {
  label: string;
  icon: string;
}

/** 操作菜单 */
const menuItems = ref<MenuItem[]>([
  { label: '修改标题', icon: 'pi pi-pen-to-square' },
  { label: '删除会话', icon: 'pi pi-trash' }
]);

const mainenuRef = ref<InstanceType<typeof Menu>>();
const openHeaderMenu = (event: Event) => {
  mainenuRef.value?.toggle(event);
};
</script>
