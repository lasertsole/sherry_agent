<template>
  <aside
    class="relative h-full overflow-hidden"
    :class="[
      collapsed ? 'w-0' : 'border-l border-solid border-gray-light dark:border-gray-dark',
      dragging ? '' : 'transition-all duration-300'
    ]"
    :style="collapsed ? undefined : { width: `${store.width}px` }">
    <!-- Resize handle on the left edge: dragging left widens the panel (it is
         right-anchored); the store clamps every step to the allowed band. The
         grip pill is the affordance — an 8px edge alone reads as a border, so
         the pill, the col-resize cursor and the tooltip say "drag me". Its
         greys are mode-independent: the theme accent resolves to white on a
         dark-mode build, which disappears over a light panel. -->
    <div
      v-if="!collapsed"
      class="group absolute left-0 top-0 h-full w-2 z-10 flex items-center justify-center cursor-col-resize"
      :class="{ 'bg-gray-400/20': dragging }"
      role="separator"
      aria-orientation="vertical"
      :aria-label="t('rightSidebar.resize')"
      :title="t('rightSidebar.resize')"
      @pointerdown="startDrag">
      <span
        class="h-8 w-1 rounded-full transition-colors"
        :class="dragging ? 'bg-gray-500' : 'bg-gray-400/70 group-hover:bg-gray-400'"></span>
    </div>
    <!-- Body keeps the expanded width while the shell animates, so collapsing
         slides it out instead of reflowing; it unmounts once the slide is over
         (a mounted panel holds its live streams open). -->
    <div
      v-if="contentMounted"
      class="flex flex-col h-full"
      :style="{ width: `${store.width}px` }">
      <!-- Tab strip: one button per open tab (each with its own ×). Tabs are
           added from the top toolbar and the settings menu. -->
      <!-- Strip height matches the session toolbar / the left sidebar's logo row, so
           all three columns share one header line. -->
      <div
        class="shrink-0 flex items-center gap-1 px-3 h-15 box-border border-b border-solid border-gray-light dark:border-gray-dark">
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
          :payload="activeTabPayload"
          :style="{ minWidth: `${PANEL_MIN_WIDTH}px`, minHeight: `${PANEL_MIN_HEIGHT}px` }"
          @saved="emit('saved')" />
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
  fileViewer: defineAsyncComponent(() => import('./FileViewerPanel.vue')),
  logs: defineAsyncComponent(() => import('./LogsPanel.vue')),
  stats: defineAsyncComponent(() => import('./StatsPanel.vue')),
  knowledgeGraph: defineAsyncComponent(() => import('./KnowledgeGraphPanel.vue')),
  skills: defineAsyncComponent(() => import('./SkillsPanel.vue')),
  systemConfig: defineAsyncComponent(() => import('./ConfigPanel.vue')),
  persona: defineAsyncComponent(() => import('./PersonaPanel.vue')),
  memory: defineAsyncComponent(() => import('./MemoryPanel.vue')),
  heartbeat: defineAsyncComponent(() => import('./HeartbeatPanel.vue')),
  cron: defineAsyncComponent(() => import('./CronPanel.vue')),
  extend: defineAsyncComponent(() => import('./ExtendPanel.vue')),
  taskDetail: defineAsyncComponent(() => import('./SubagentTasksPanel.vue'))
};

/**
 * Panels that save session-scoped content (system config, persona, memory,
 * heartbeat) report it so the shell can re-read the session's character
 * snapshot — the shell used to listen on the dialogs directly.
 */
const emit = defineEmits<{ saved: [] }>();

/** Laying-out floor of a mounted panel (the body scrolls below it). */
const PANEL_MIN_WIDTH = RIGHT_SIDEBAR_PANEL_MIN_WIDTH;
const PANEL_MIN_HEIGHT = RIGHT_SIDEBAR_PANEL_MIN_HEIGHT;

/** The active tab's kind, or null (no tabs / stale id). */
const activeKind = computed<RightSidebarPanelKind | null>(
  () => store.tabs.find(tab => tab.id === store.activeTabId)?.kind ?? null
);

/** The panel component to render (null → the empty state). */
const activePanel = computed<Component | null>(() => (activeKind.value ? PANELS[activeKind.value] : null));
/** The active tab's own data (e.g. the file viewer's path); undefined for others. */
const activeTabPayload = computed<{ path: string } | undefined>(
  () => store.tabs.find(tab => tab.id === store.activeTabId)?.payload
);

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
      "skills": "技能",
      "systemConfig": "系统配置",
      "persona": "AI人格",
      "memory": "记忆",
      "heartbeat": "心跳",
      "cron": "定时任务",
      "extend": "扩展",
      "taskDetail": "任务详情",
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
      "skills": "Skills",
      "systemConfig": "System Config",
      "persona": "Persona",
      "memory": "Memory",
      "heartbeat": "Heartbeat",
      "cron": "Scheduled Tasks",
      "extend": "Extend",
      "taskDetail": "Task details",
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
      "skills": "スキル",
      "systemConfig": "システム設定",
      "persona": "AI ペルソナ",
      "memory": "メモリ",
      "heartbeat": "ハートビート",
      "cron": "定期タスク",
      "extend": "拡張",
      "taskDetail": "タスク詳細",
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
      "skills": "스킬",
      "systemConfig": "시스템 설정",
      "persona": "AI 페르소나",
      "memory": "메모리",
      "heartbeat": "하트비트",
      "cron": "예약 작업",
      "extend": "확장",
      "taskDetail": "작업 상세",
      "knowledgeGraph": "지식 그래프",
      "resize": "드래그하여 너비 조절",
      "stats": "통계",
      "closeTab": "탭 닫기",
      "empty": "탭이 없습니다. 메뉴 또는 툴바에서 추가"
    }
  }
}
</i18n>
