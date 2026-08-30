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
  <button
    type="button"
    class="block sm:hidden cursor-pointer"
    :aria-label="t('a11y.toggleTheme')"
    @click="handleSwitch(currentMode === 'dark' ? 'light' : 'dark')">
    <i :class="['pi', { 'pi-moon': currentMode === 'dark', 'pi-sun': currentMode === 'light' }]"></i>
  </button>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
/** Color mode */
const colorMode = useColorMode();
/** Global UI store (unified write entry for theme switching) */
const uiStore = useUiStore();
/** Current mode */
const currentMode = ref<string>(colorMode.preference);

/** Switch the theme */
const handleSwitch = (value: string | boolean) => {
  // ToggleSwitch's value-change emits a boolean; normalize both shapes to the mode string.
  const mode = typeof value === 'string' ? value : value ? 'dark' : 'light';
  currentMode.value = mode;
  uiStore.setTheme(mode);
};
</script>
