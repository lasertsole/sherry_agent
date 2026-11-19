<template>
  <!-- Left side - data-driven navigation shell (UI upgrade S1).
       The column chrome (width / resize / collapse) lives here; the body is a
       nav-registry component kept alive so each panel preserves its own state. -->
  <div
    :class="[
      'relative h-full overflow-hidden transition-all duration-300',
      collapsed
        ? 'w-0 border-r-0'
        : 'shrink-0 border-r border-solid border-gray-light bg-transparent dark:border-gray-dark dark:bg-transparent'
    ]"
    :style="{ width: collapsed ? '0px' : displayWidth + 'px' }"
    data-test="session-sidebar">
    <div
      v-if="!collapsed && extraWidth <= 0"
      class="group absolute right-0 top-0 z-10 flex h-full w-2 items-center justify-center cursor-col-resize"
      :class="{ 'bg-gray-400/20': resizing }"
      role="separator"
      aria-orientation="vertical"
      :aria-label="t('sidebar.resize')"
      :title="t('sidebar.resize')"
      data-test="session-sidebar-resize"
      @pointerdown="startResize">
      <span
        class="h-8 w-1 rounded-full transition-colors"
        :class="resizing ? 'bg-gray-500' : 'bg-gray-400/70 group-hover:bg-gray-400'" />
    </div>
    <div
      class="flex flex-col px-4 h-full"
      :style="{ width: displayWidth + 'px' }">
      <!-- LOGO area -->
      <div
        class="flex items-center h-15 text-xl"
        data-test="sidebar-title-row">
        <span class="truncate">🍊{{ t('chatBox.defaultAiName') }}</span>
      </div>
      <!-- Navigation icon row: data-driven from the nav registry. -->
      <div
        class="nav-icons flex shrink-0 gap-1 px-2 py-1 overflow-x-auto"
        data-test="nav-icons">
        <button
          v-for="item in nav.items"
          :key="item.id"
          type="button"
          class="relative flex items-center justify-center w-8 h-8 rounded transition-colors"
          :class="
            item.id === nav.activeId
              ? 'bg-theme-main/10 text-theme-main'
              : 'text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'
          "
          :aria-pressed="item.id === nav.activeId"
          :title="t(item.labelKey)"
          :aria-label="t(item.labelKey)"
          :data-test="`nav-${item.id}`"
          @click="nav.activate(item.id)">
          <i :class="item.icon" />
          <span
            v-if="item.badge && item.badge() > 0"
            class="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-1 rounded-full bg-red-500 text-white text-[10px] leading-4 text-center"
            data-test="nav-badge"
            >{{ item.badge() }}</span
          >
        </button>
      </div>
      <!-- Nav content: KeepAlive preserves scroll/filter state across switches. -->
      <div
        class="nav-content flex min-h-0 flex-1 flex-col"
        data-test="nav-content">
        <KeepAlive :max="5">
          <component
            :is="nav.activeItem.component ?? NotImplementedPanel"
            v-if="nav.activeItem"
            :key="nav.activeId"
            v-model:current-session-id="currentSessionId"
            :extra-width="extraWidth" />
        </KeepAlive>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
/**
 * Re-exported for the shell/preset-dialog consumers that import it from this
 * module. The implementation lives in a plain module so the session-list panel
 * can use it without a circular import (see session-character.ts).
 */
export { ensureSessionCharacter } from './session-character';
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { RIGHT_SIDEBAR_MIN_WIDTH, useRightSidebarStore } from '@/stores/right-sidebar';
import { useSidebarWidthStore } from '@/stores/sidebar-width';
import { useNavStore } from '@/stores/nav';
import NotImplementedPanel from '@/components/NotImplementedPanel.vue';

const { t } = useI18n();
const nav = useNavStore();
const collapsed = defineModel<boolean>('collapsed', { default: false });
const currentSessionId = defineModel<string | undefined>('currentSessionId');
const props = withDefaults(defineProps<{ extraWidth?: number }>(), { extraWidth: 0 });

const sidebarWidthStore = useSidebarWidthStore();
const sidebarWidth = computed(() => sidebarWidthStore.width);
const displayWidth = computed(() => sidebarWidth.value + Math.max(0, props.extraWidth));
const rightSidebar = useRightSidebarStore();
const shellReservedWidth = computed(() => 320 + (rightSidebar.collapsed ? 0 : RIGHT_SIDEBAR_MIN_WIDTH));

const resizing = ref(false);
let resizeStartX = 0;
let resizeStartWidth = 0;

const onResizeMove = (event: PointerEvent) => {
  if (!resizing.value) return;
  sidebarWidthStore.setWidth(
    resizeStartWidth + (event.clientX - resizeStartX),
    window.innerWidth,
    shellReservedWidth.value
  );
};
const stopResize = () => {
  if (!resizing.value) return;
  resizing.value = false;
  window.removeEventListener('pointermove', onResizeMove);
  window.removeEventListener('pointerup', stopResize);
  window.removeEventListener('pointercancel', stopResize);
};
const startResize = (event: PointerEvent) => {
  resizing.value = true;
  resizeStartX = event.clientX;
  resizeStartWidth = sidebarWidth.value;
  window.addEventListener('pointermove', onResizeMove);
  window.addEventListener('pointerup', stopResize);
  window.addEventListener('pointercancel', stopResize);
  event.preventDefault();
};
const fitWidthToViewport = () => sidebarWidthStore.fitToViewport(window.innerWidth, shellReservedWidth.value);

onMounted(() => {
  fitWidthToViewport();
  window.addEventListener('resize', fitWidthToViewport);
});
onBeforeUnmount(() => {
  stopResize();
  window.removeEventListener('resize', fitWidthToViewport);
});
</script>
