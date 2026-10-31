<template>
  <aside
    class="h-full overflow-hidden"
    :class="[
      store.maximized
        ? // `fixed` must REPLACE `relative`: both are the same-specificity
          // position utility, and the sheet's order (relative after fixed) would
          // otherwise keep the panel in flow and leave it at its min width.
          'fixed inset-0 z-40 bg-white dark:bg-[#181820]'
        : collapsed
          ? 'relative w-0'
          : // `shrink` lets the column give way when the window is too narrow for
            // every minimum (the left column is `shrink-0` and the conversation
            // has its own 320px floor); the inline width stays the target.
            'relative shrink border-l border-solid border-gray-light min-w-[280px] dark:border-gray-dark',
      dragging ? '' : 'transition-all duration-300'
    ]"
    :style="collapsed || store.maximized ? undefined : { width: `${displayWidth}px` }">
    <!-- Resize handle on the left edge: dragging left widens the panel (it is
         right-anchored); the store clamps every step to the allowed band. The
         grip pill is the affordance — an 8px edge alone reads as a border, so
         the pill, the col-resize cursor and the tooltip say "drag me". Its
         greys are mode-independent: the theme accent resolves to white on a
         dark-mode build, which disappears over a light panel. Hidden while the
         chat column is floated: the extra width is a presentation override, so
         a drag would move a width the user cannot see. -->
    <div
      v-if="!collapsed && !store.maximized && props.extraWidth <= 0"
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
      :style="store.maximized ? { width: '100%' } : { width: `${displayWidth}px` }">
      <!-- Strip: the group pills (This session / Global — the tabs' scope) are FIXED at the
           left of the single tab row, and the active group's tab list scrolls to
           their right. The row keeps the shared h-15, so all three columns still
           share one header line. Tabs are added from the top toolbar and the
           settings menu. -->
      <div
        class="shrink-0 flex items-center gap-2 px-3 h-15 box-border border-b border-solid border-gray-light dark:border-gray-dark">
        <!-- Recently closed tabs: the down arrow is the LEFTMOST control in the
             strip; its menu offers the same two groups (当前会话 / 全局). A closed
             side chat restores in place with its retained history. -->
        <RecentClosedTabsMenu :session-id="sessionId" />
        <!-- Maximize / restore, LEFT of the group pills: the panel fills the whole
             viewport so a wide editor (模型配置, 预设, 技能) does not have to be read
             through its narrow column; the same button restores the previous
             layout (nothing else is mutated — the panel becomes a fixed overlay). -->
        <button
          type="button"
          class="shrink-0 flex h-6 w-6 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-theme-main dark:text-gray-400 dark:hover:bg-gray-800"
          data-test="sidebar-maximize"
          :title="t(store.maximized ? 'rightSidebar.restore' : 'rightSidebar.maximize')"
          :aria-label="t(store.maximized ? 'rightSidebar.restore' : 'rightSidebar.maximize')"
          :aria-pressed="store.maximized"
          @click="store.toggleMaximized()">
          <i
            :class="store.maximized ? 'pi pi-window-minimize' : 'pi pi-window-maximize'"
            class="text-xs"></i>
        </button>
        <div
          class="flex shrink-0 items-center gap-0.5 rounded-full border border-solid border-gray-200/80 bg-gray-100/80 p-0.5 dark:border-gray-700/80 dark:bg-gray-800/80"
          data-test="scope-tabs">
          <button
            v-for="scope in SCOPES"
            :key="scope"
            type="button"
            class="h-6 rounded-full px-2.5 text-xs transition-all duration-300"
            :class="
              scope === store.activeScope
                ? 'bg-white font-medium text-theme-main shadow-sm dark:bg-[#2a2a36]'
                : 'text-gray-500 hover:text-theme-main dark:text-gray-400'
            "
            :data-test="`scope-tab-${scope}`"
            @click="store.setActiveScope(scope)">
            {{ t(`rightSidebar.scopes.${scope}`) }}
          </button>
        </div>
        <!-- Tabs are EQUAL width and shrink proportionally: a 156px basis with
             a 60px floor, so a long title never makes a tab wider and a crowded
             strip narrows every tab by the same amount before it starts
             scrolling. The fade masks (absolute, outside the scroller) mark the
             side a scrolled strip is cutting off; the title fades instead of
             ending in an ellipsis. -->
        <div class="relative min-w-0 flex-1">
          <div
            ref="tabsViewport"
            class="flex h-full min-w-0 items-center gap-1 overflow-x-auto scrollbar-hide"
            :data-overflowing="isOverflowing ? 'true' : 'false'"
            data-test="tabs-viewport">
            <button
              v-for="tab in store.tabsInScope(store.activeScope)"
              :key="tab.id"
              type="button"
              class="group flex h-7 min-w-15 max-w-39 flex-[1_1_9.75rem] items-center gap-1 overflow-hidden whitespace-nowrap rounded-md px-1.5 pr-2 text-xs transition-colors"
              :class="
                tab.id === store.activeTabId
                  ? 'bg-theme-main/10 text-theme-main'
                  : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'
              "
              :title="tabLabel(tab)"
              :draggable="isDraggableTab(tab)"
              :data-test="`tab-${tab.kind}`"
              @click="store.activateTab(tab.id)"
              @dragstart="onTabDragStart($event, tab)">
              <span
                class="min-w-0 flex-1 overflow-hidden whitespace-nowrap"
                :style="TAB_TITLE_MASK"
                >{{ tabLabel(tab) }}</span
              >
              <!-- The close X is always present in the LAYOUT (so the title does
                   not jump on hover) and fades in for the active tab and on
                   hover. -->
              <i
                class="pi pi-times shrink-0 text-[10px] transition-opacity"
                :class="
                  tab.id === store.activeTabId
                    ? 'opacity-100 hover:opacity-60'
                    : 'opacity-0 group-hover:opacity-60 hover:opacity-100'
                "
                :aria-label="t('rightSidebar.closeTab')"
                :data-test="`tab-close-${tab.kind}`"
                @click.stop="emit('close-tab', tab)"></i>
            </button>
            <span
              v-if="store.tabsInScope(store.activeScope).length === 0"
              class="text-xs text-gray-400 dark:text-gray-500"
              data-test="scope-empty">
              {{ t('rightSidebar.scopeEmpty') }}
            </span>
          </div>
          <!-- Fade masks: pointer-events-none, anchored to the WRAPPER (inside
               the scroller they would scroll away with the content). -->
          <div
            v-if="showLeftMask"
            class="pointer-events-none absolute inset-y-0 left-0 z-10 w-4 bg-gradient-to-r from-white to-transparent dark:from-[#181820]"
            data-test="tabs-mask-left" />
          <div
            v-if="showRightMask"
            class="pointer-events-none absolute inset-y-0 right-0 z-10 w-4 bg-gradient-to-l from-white to-transparent dark:from-[#181820]"
            data-test="tabs-mask-right" />
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
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, useTemplateRef, watch } from 'vue';
import type { Component } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  RIGHT_SIDEBAR_PANEL_MIN_HEIGHT,
  RIGHT_SIDEBAR_PANEL_MIN_WIDTH,
  type RightSidebarPanelKind,
  type RightSidebarScope,
  type RightSidebarTab
} from '~/stores/right-sidebar';
import { useSidebarWidthStore } from '~/stores/sidebar-width';
import { useUiStore } from '~/stores/ui';
import type { FileMentionPayload } from '~/composables/use-file-mention';
import { middleMinWidth } from '@/common/layout';
import RecentClosedTabsMenu from './RecentClosedTabsMenu.vue';

/** The session the main area shows (the recent-tabs menu filters by it). */
const props = withDefaults(defineProps<{ sessionId?: string; extraWidth?: number }>(), { extraWidth: 0 });

const { t } = useI18n({ useScope: 'local' });

const store = useRightSidebarStore();
/**
 * What the DOM renders: the stored width plus the share the shell hands over
 * while the conversation column is floated. Presentation-only — the store is
 * never written — so docking the chat restores this column exactly.
 */
const displayWidth = computed(() => store.width + Math.max(0, props.extraWidth));
/**
 * Pixels the left column plus the conversation's floor need. The drag ceiling
 * subtracts them so the stored width is always one the flex row can actually
 * render — without it a wide drag stored 65% while the DOM stopped sooner.
 */
const leftSidebar = useSidebarWidthStore();
const ui = useUiStore();
const reservedWidth = () => (ui.sidebarCollapsed ? 0 : leftSidebar.width) + middleMinWidth(window.innerWidth);

/**
 * A tab's strip label: its kind's name, plus `` N`` when several tabs of that
 * kind are open (the toolbox opens a browser / terminal per click, and two tabs
 * both reading Browser would be indistinguishable).
 * @param tab
 * @param tab.id
 * @param tab.kind
 */
const tabLabel = (tab: RightSidebarTab): string => {
  // Count only the SAME visible group: twins in another session must not shift
  // this session's numbering.
  const sameKind = store.tabsInScope(tab.scope).filter(candidate => candidate.kind === tab.kind);
  const base = t(`rightSidebar.${tab.kind}`);
  if (sameKind.length <= 1) return base;
  return `${base} ${sameKind.findIndex(candidate => candidate.id === tab.id) + 1}`;
};

/** The group tabs, in display order (session first, then the global tools). */
const SCOPES: ReadonlyArray<RightSidebarScope> = ['session', 'global'];

/**
 * Fade the title's last few pixels instead of truncating it with an ellipsis.
 *
 * The span is the tab's flexible item (`min-w-0 flex-1`), so 100% is the
 * visible width already excluding the close button — no per-state offset is
 * needed (an offset would only slide the fade on hover, which reads as the
 * title shifting).
 */
const TAB_TITLE_MASK: Record<string, string> = {
  maskImage: 'linear-gradient(to right, black calc(100% - 0.5rem), transparent)',
  WebkitMaskImage: 'linear-gradient(to right, black calc(100% - 0.5rem), transparent)'
};

/** The tab row's scroller (overflow amount + fade sides). */
const tabsViewport = useTemplateRef<HTMLElement>('tabsViewport');
/** Tabs currently on screen: the active group's list. */
const visibleTabCount = computed(() => store.tabsInScope(store.activeScope).length);
const { isOverflowing, showLeftMask, showRightMask } = useTabsOverflow(tabsViewport, visibleTabCount);

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
  webSearch: defineAsyncComponent(() => import('./WebSearchPanel.vue')),
  taskDetail: defineAsyncComponent(() => import('./SubagentTasksPanel.vue')),
  account: defineAsyncComponent(() => import('./AccountSettingsPanel.vue')),
  notification: defineAsyncComponent(() => import('./NotificationPanel.vue')),
  sessionPreset: defineAsyncComponent(() => import('./SessionPresetPanel.vue')),
  gitDiff: defineAsyncComponent(() => import('./GitDiffPanel.vue')),
  contextViewer: defineAsyncComponent(() => import('./ContextViewerPanel.vue')),
  trajectory: defineAsyncComponent(() => import('./TrajectoryPanel.vue')),
  tokenBudget: defineAsyncComponent(() => import('./TokenBudgetPanel.vue')),
  browser: defineAsyncComponent(() => import('./BrowserPanel.vue')),
  terminal: defineAsyncComponent(() => import('./TerminalPanel.vue')),
  whiteboard: defineAsyncComponent(() => import('./WhiteboardPanel.vue')),
  sideChat: defineAsyncComponent(() => import('./SideChatPanel.vue'))
};

/**
 * Panels that save session-scoped content (system config, persona, memory,
 * heartbeat) report it so the shell can re-read the session's character
 * snapshot — the shell used to listen on the dialogs directly.
 */
const emit = defineEmits<{ saved: []; 'close-tab': [tab: RightSidebarTab] }>();

/** Laying-out floor of a mounted panel (the body scrolls below it). */
const PANEL_MIN_WIDTH = RIGHT_SIDEBAR_PANEL_MIN_WIDTH;
const PANEL_MIN_HEIGHT = RIGHT_SIDEBAR_PANEL_MIN_HEIGHT;

/** The active tab, or null (no tabs / stale id / a tab of another session). */
const activeTab = computed(() => {
  const tab = store.tabs.find(candidate => candidate.id === store.activeTabId) ?? null;
  if (tab && tab.scope === 'session' && tab.sessionId !== undefined && tab.sessionId !== store.activeSessionId) {
    return null;
  }
  return tab;
});

/**
 * The active tab's kind, or null. Null also when the active tab belongs to the
 * OTHER group than the one being browsed: a group switch shows that group's tabs
 * (its empty state when it has none), never the other group's panel.
 */
const activeKind = computed<RightSidebarPanelKind | null>(() =>
  activeTab.value && activeTab.value.scope === store.activeScope ? activeTab.value.kind : null
);

/** The panel component to render (null → the empty state). */
const activePanel = computed<Component | null>(() => (activeKind.value ? PANELS[activeKind.value] : null));
/** The active tab's own data (the file viewer's path, a toolbox panel's
 *  instance key…); undefined for the kinds that carry none. */
const activeTabPayload = computed<
  | {
      path?: string;
      hash?: string;
      instance?: string;
      url?: string;
      sessionId?: string;
      boardId?: string;
      parentSessionId?: string;
      inherited?: boolean;
      quote?: string;
      source?: string;
      prefillQuote?: boolean;
    }
  | undefined
>(() => activeTab.value?.payload);

/**
 * Whether a tab can be dragged into the composer as a file mention. Only the
 * file viewer carries a workspace path; the settings editors have nothing to
 * reference.
 * @param tab
 */
const isDraggableTab = (tab: RightSidebarTab): boolean => tab.kind === 'fileViewer' && !!tab.payload?.path;

/**
 * Start a drag carrying the tab's file as a mention (the composer's drop
 * handler reads the custom MIME and appends a chip).
 * @param event
 * @param tab
 */
const onTabDragStart = (event: DragEvent, tab: RightSidebarTab): void => {
  const path = tab.payload?.path;
  if (!path || !event.dataTransfer) return;
  const payload: FileMentionPayload = {
    type: 'file',
    path,
    name: path.split('/').pop() ?? path
  };
  event.dataTransfer.setData(FILE_MENTION_MIME, JSON.stringify(payload));
  event.dataTransfer.setData('text/plain', buildMentionText(payload));
  event.dataTransfer.effectAllowed = 'copy';
};

/** Keep the store usable from the template without unwrapping refs manually. */
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
  store.setWidth(dragStartWidth - (event.clientX - dragStartX), window.innerWidth, reservedWidth());
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
const fitToViewport = () => store.fitToViewport(window.innerWidth, reservedWidth());

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
      "fileViewer": "项目文件",
      "account": "账户",
      "logs": "日志查看",
      "skills": "技能",
      "systemConfig": "系统配置",
      "persona": "预设角色",
      "notification": "通知",
      "maximize": "放大到整个视口",
      "restore": "还原面板宽度",
      "memory": "记忆",
      "heartbeat": "心跳",
      "cron": "定时任务",
      "extend": "扩展",
      "webSearch": "模型联网",
      "taskDetail": "任务详情",
      "knowledgeGraph": "知识图谱",
      "resize": "拖动调整宽度",
      "stats": "统计",
      "closeTab": "关闭标签页",
      "empty": "暂无标签页，从菜单或工具栏添加",
      "sessionPreset": "预设角色",
      "gitDiff": "提交差异",
      "contextViewer": "Agent 上下文",
      "trajectory": "轨迹",
      "browser": "浏览器",
      "terminal": "终端",
      "scopes": {
        "session": "当前会话",
        "global": "全局"
      },
      "scopeEmpty": "该分组暂无标签页"
    }
  },
  "en": {
    "rightSidebar": {
      "fileViewer": "Project files",
      "account": "Account",
      "logs": "Logs",
      "skills": "Skills",
      "systemConfig": "System Config",
      "persona": "Presets & Role",
      "notification": "Notifications",
      "maximize": "Expand to the full viewport",
      "restore": "Restore the panel width",
      "memory": "Memory",
      "heartbeat": "Heartbeat",
      "cron": "Scheduled Tasks",
      "extend": "Extend",
      "webSearch": "Web Search",
      "taskDetail": "Task details",
      "knowledgeGraph": "Knowledge Graph",
      "resize": "Drag to resize",
      "stats": "Statistics",
      "closeTab": "Close tab",
      "empty": "No tabs yet — add one from the menu or toolbar",
      "sessionPreset": "Presets & Role",
      "gitDiff": "Commit diff",
      "contextViewer": "Agent context",
      "trajectory": "Trajectory",
      "browser": "Browser",
      "terminal": "Terminal",
      "scopes": {
        "session": "This session",
        "global": "Global"
      },
      "scopeEmpty": "No tabs in this group"
    }
  },
  "ja": {
    "rightSidebar": {
      "fileViewer": "プロジェクトファイル",
      "account": "アカウント",
      "logs": "ログ表示",
      "skills": "スキル",
      "systemConfig": "システム設定",
      "persona": "プリセットと役割",
      "notification": "通知",
      "maximize": "ビューポート全体に拡大",
      "restore": "パネル幅に戻す",
      "memory": "メモリ",
      "heartbeat": "ハートビート",
      "cron": "定期タスク",
      "extend": "拡張",
      "webSearch": "モデル検索",
      "taskDetail": "タスク詳細",
      "knowledgeGraph": "ナレッジグラフ",
      "resize": "ドラッグで幅を変更",
      "stats": "統計",
      "closeTab": "タブを閉じる",
      "empty": "タブがありません。メニューまたはツールバーから追加",
      "sessionPreset": "プリセットと役割",
      "gitDiff": "コミット差分",
      "contextViewer": "エージェント コンテキスト",
      "trajectory": "軌跡",
      "browser": "ブラウザ",
      "terminal": "ターミナル",
      "scopes": {
        "session": "現在のセッション",
        "global": "グローバル"
      },
      "scopeEmpty": "このグループにタブはありません"
    }
  },
  "ko": {
    "rightSidebar": {
      "fileViewer": "프로젝트 파일",
      "account": "계정",
      "logs": "로그 보기",
      "skills": "스킬",
      "systemConfig": "시스템 설정",
      "persona": "프리셋과 역할",
      "notification": "알림",
      "maximize": "뷰포트 전체로 확대",
      "restore": "패널 너비로 복원",
      "memory": "메모리",
      "heartbeat": "하트비트",
      "cron": "예약 작업",
      "extend": "확장",
      "webSearch": "모델 검색",
      "taskDetail": "작업 상세",
      "knowledgeGraph": "지식 그래프",
      "resize": "드래그하여 너비 조절",
      "stats": "통계",
      "closeTab": "탭 닫기",
      "empty": "탭이 없습니다. 메뉴 또는 툴바에서 추가",
      "sessionPreset": "프리셋과 역할",
      "gitDiff": "커밋 diff",
      "contextViewer": "에이전트 컨텍스트",
      "trajectory": "궤적",
      "browser": "브라우저",
      "terminal": "터미널",
      "scopes": {
        "session": "현재 세션",
        "global": "전역"
      },
      "scopeEmpty": "이 그룹에 탭이 없습니다"
    }
  }
}
</i18n>
