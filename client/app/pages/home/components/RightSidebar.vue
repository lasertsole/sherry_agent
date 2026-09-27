<template>
  <aside
    class="relative h-full overflow-hidden"
    :class="[
      collapsed ? 'w-0' : 'border-l border-solid border-gray-light dark:border-gray-dark',
      dragging ? '' : 'transition-all duration-300'
    ]"
    :style="collapsed ? undefined : { width: `${store.width}px` }">
    <!-- Drag handle on the left edge: dragging left widens the panel (it is
         right-anchored); the store clamps every step to the allowed band. -->
    <div
      v-if="!collapsed"
      class="absolute left-0 top-0 h-full w-1.5 z-10 cursor-col-resize hover:bg-theme-main/30"
      :class="{ 'bg-theme-main/40': dragging }"
      role="separator"
      aria-orientation="vertical"
      :aria-label="t('rightSidebar.resize')"
      @pointerdown="startDrag"></div>
    <!-- Body keeps the expanded width while the shell animates, so collapsing
         slides it out instead of reflowing; it unmounts once the slide is over
         (a mounted panel holds its live streams open). -->
    <div
      v-if="contentMounted"
      class="flex flex-col h-full"
      :style="{ width: `${store.width}px` }">
      <!-- Tab strip: one button per open tab (each with its own ×). Tabs are
           added from the top toolbar and the settings menu. -->
      <div
        class="shrink-0 flex items-center gap-1 px-2 h-10 box-border border-b border-solid border-gray-light dark:border-gray-dark">
        <div class="flex-1 min-w-0 flex items-center gap-1 overflow-x-auto">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            type="button"
            class="group shrink-0 flex items-center gap-1 h-7 px-2 rounded-md text-xs transition-colors"
            :class="
              tab.id === store.activeTabId
                ? 'bg-theme-main/10 text-theme-main'
                : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'
            "
            :title="t(`rightSidebar.${tab.kind}`)"
            @click="store.activateTab(tab.id)">
            <span class="truncate max-w-24">{{ t(`rightSidebar.${tab.kind}`) }}</span>
            <i
              class="pi pi-times text-[10px] opacity-60 hover:opacity-100"
              :aria-label="t('rightSidebar.closeTab')"
              @click.stop="store.closeTab(tab.id)"></i>
          </button>
        </div>
      </div>

      <!-- Panel area: scrolls on BOTH axes, so a panel that does not fit the
           current width/height scrolls instead of being squeezed or clipped.
           The panel is lazily imported and mounted while its tab is active
           (unmounting tears its streams down); an empty hint shows otherwise. -->
      <div class="flex-1 min-h-0 overflow-auto">
        <!-- No KeepAlive on purpose: unmounting the panel closes its live
             streams, and re-adding a tab starts them again. -->
        <component
          :is="activePanel"
          v-if="activePanel"
          :key="store.activeTabId ?? 'none'"
          class="h-full"
          :style="{ minWidth: `${PANEL_MIN_WIDTH}px`, minHeight: `${PANEL_MIN_HEIGHT}px` }" />
        <div
          v-if="!activePanel"
          class="h-full flex flex-col items-center justify-center gap-2 px-4 text-center text-xs text-gray-500 dark:text-gray-400">
          <i class="pi pi-window-maximize text-lg"></i>
          <span>{{ t('rightSidebar.empty') }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { Component } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  RIGHT_SIDEBAR_PANEL_MIN_HEIGHT,
  RIGHT_SIDEBAR_PANEL_MIN_WIDTH,
  type RightSidebarPanelKind
} from '~/stores/right-sidebar';

const { t } = useI18n({ useScope: 'local' });

const store = useRightSidebarStore();

/** Lazy panel components: the chunk loads when a tab of that kind first mounts. */
const PANELS: Record<RightSidebarPanelKind, Component> = {
  logs: defineAsyncComponent(() => import('./LogsPanel.vue')),
  stats: defineAsyncComponent(() => import('./StatsPanel.vue')),
  knowledgeGraph: defineAsyncComponent(() => import('./KnowledgeGraphPanel.vue'))
};

/** Laying-out floor of a mounted panel (the body scrolls below it). */
const PANEL_MIN_WIDTH = RIGHT_SIDEBAR_PANEL_MIN_WIDTH;
const PANEL_MIN_HEIGHT = RIGHT_SIDEBAR_PANEL_MIN_HEIGHT;

/** The active tab's kind, or null (no tabs / stale id). */
const activeKind = computed<RightSidebarPanelKind | null>(
  () => store.tabs.find(tab => tab.id === store.activeTabId)?.kind ?? null
);

/** The panel component to render (null → the empty state). */
const activePanel = computed<Component | null>(() => (activeKind.value ? PANELS[activeKind.value] : null));

/** Keep the store usable from the template without unwrapping refs manually. */
const tabs = computed(() => store.tabs);
const collapsed = computed(() => store.collapsed);

/**
 * Whether the body is in the DOM. It stays mounted for the length of the
 * collapse animation (the slide-out needs something to slide) and is removed
 * once the shell has reached `w-0`, which is what tears the panel's streams
 * down. Expanding re-mounts it immediately so the panel slides back in.
 */
const contentMounted = ref(!collapsed.value);
/** Matches the shell's `duration-300`, plus a frame so the slide is not cut short. */
const COLLAPSE_ANIMATION_MS = 320;
let unmountTimer: ReturnType<typeof setTimeout> | undefined;

watch(collapsed, isCollapsed => {
  if (unmountTimer) clearTimeout(unmountTimer);
  if (!isCollapsed) {
    contentMounted.value = true;
    return;
  }
  unmountTimer = setTimeout(() => {
    contentMounted.value = false;
  }, COLLAPSE_ANIMATION_MS);
});

/** Width drag: pointer position at pointerdown + the width it started from. */
const dragging = ref(false);
let dragStartX = 0;
let dragStartWidth = 0;

/**
 * Follow the pointer: the panel is right-anchored, so moving LEFT (dx < 0)
 * widens it. `setWidth` clamps each step into the allowed band, so the drag
 * can never exceed the maximum or collapse the panel below its minimum.
 * @param event Pointer move event.
 */
const onDragMove = (event: PointerEvent) => {
  if (!dragging.value) return;
  store.setWidth(dragStartWidth - (event.clientX - dragStartX), window.innerWidth);
};

/** End the drag and detach the window listeners. */
const stopDrag = () => {
  if (!dragging.value) return;
  dragging.value = false;
  window.removeEventListener('pointermove', onDragMove);
  window.removeEventListener('pointerup', stopDrag);
  window.removeEventListener('pointercancel', stopDrag);
};

/**
 * Start a width drag from the left-edge handle.
 * @param event Pointer down event on the handle.
 */
const startDrag = (event: PointerEvent) => {
  dragging.value = true;
  dragStartX = event.clientX;
  dragStartWidth = store.width;
  window.addEventListener('pointermove', onDragMove);
  window.addEventListener('pointerup', stopDrag);
  window.addEventListener('pointercancel', stopDrag);
  event.preventDefault();
};

/**
 * Keep the panel inside its band when the window is resized: a width saved on a
 * wide window would otherwise overflow a narrower one.
 */
const fitToViewport = () => store.fitToViewport(window.innerWidth);

onMounted(() => {
  fitToViewport();
  window.addEventListener('resize', fitToViewport);
});

onBeforeUnmount(() => {
  stopDrag();
  window.removeEventListener('resize', fitToViewport);
  if (unmountTimer) clearTimeout(unmountTimer);
});
</script>

<i18n lang="json">
{
  "zh": {
    "rightSidebar": {
      "logs": "日志查看",
      "knowledgeGraph": "知识图谱",
      "resize": "拖动调整宽度",
      "stats": "统计",
      "closeTab": "关闭标签页",
      "empty": "暂无标签页，从菜单或工具栏添加"
    }
  },
  "en": {
    "rightSidebar": {
      "logs": "Logs",
      "knowledgeGraph": "Knowledge Graph",
      "resize": "Drag to resize",
      "stats": "Statistics",
      "closeTab": "Close tab",
      "empty": "No tabs yet — add one from the menu or toolbar"
    }
  },
  "ja": {
    "rightSidebar": {
      "logs": "ログ表示",
      "knowledgeGraph": "ナレッジグラフ",
      "resize": "ドラッグで幅を変更",
      "stats": "統計",
      "closeTab": "タブを閉じる",
      "empty": "タブがありません。メニューまたはツールバーから追加"
    }
  },
  "ko": {
    "rightSidebar": {
      "logs": "로그 보기",
      "knowledgeGraph": "지식 그래프",
      "resize": "드래그하여 너비 조절",
      "stats": "통계",
      "closeTab": "탭 닫기",
      "empty": "탭이 없습니다. 메뉴 또는 툴바에서 추가"
    }
  }
}
</i18n>
