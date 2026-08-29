<template>
  <ToggleSwitch
    class="hidden sm:block"
    trueValue="dark"
    falseValue="light"
    v-model="currentMode"
    @value-change="handleSwitch">
    <template #handle="{ checked }">
      <i :class="['!text-xs pi', { 'pi-moon': checked, 'pi-sun': !checked }]"></i>
    </template>
  </ToggleSwitch>
  <i
    :class="['block sm:hidden pi', { 'pi-moon': currentMode === 'dark', 'pi-sun': currentMode === 'light' }]"
    @click="handleSwitch(currentMode === 'dark' ? 'light' : 'dark')"></i>
</template>

<script setup lang="ts">
/** 颜色主题 */
const colorMode = useColorMode();
/** UI 全局 store（主题切换统一写入口） */
const uiStore = useUiStore();
/** 当前模式 */
const currentMode = ref<string>(colorMode.preference);

/** 切换主题 */
const handleSwitch = (value: string) => {
  currentMode.value = value;
  uiStore.setTheme(value);
};
</script>
