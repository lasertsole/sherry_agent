<template>
  <!-- Left side - history records area -->
  <!-- In-flow column whose width is a persisted store value (drag the right
       edge to resize: 280px floor, half the viewport at most; the fresh-profile
       default is 360px on a desktop-width window and 280px below it).
       Collapsed = width 0, the outer overflow-hidden clips the fixed inner
       width wholesale. -->
  <div
    :class="[
      'relative h-full overflow-hidden transition-all duration-300',
      collapsed
        ? 'w-0 border-r-0'
        : 'shrink-0 border-r border-solid border-gray-light bg-transparent dark:border-gray-dark dark:bg-transparent'
    ]"
    :style="{ width: collapsed ? '0px' : displayWidth + 'px' }"
    data-test="session-sidebar">
    <!-- Right-edge resize handle: dragging right widens the column (it is
         left-anchored). The store clamps every step, so the drag can never go
         below the 280px floor or past half the viewport. Hidden while
         collapsed — there is no width to drag — and while the chat column is
         floated (the extra width is a presentation override, not this column's
         stored width, so a drag would move a width the user cannot see). -->
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
        :class="resizing ? 'bg-gray-500' : 'bg-gray-400/70 group-hover:bg-gray-400'"></span>
    </div>
    <!-- Content width follows the column so the list reflows while dragging;
         when collapsed the outer overflow-hidden clips it wholesale. -->
    <div
      class="flex flex-col px-4 h-full"
      :style="{ width: displayWidth + 'px' }">
      <!-- LOGO area (the theme switch and the language picker live in the top
           toolbar, immediately left of the menu button). -->
      <div
        class="flex items-center h-15 text-xl"
        data-test="sidebar-title-row">
        <span class="truncate">🍊{{ t('chatBox.defaultAiName') }}</span>
      </div>
      <!-- Left-sidebar body switch: the session list (default) or the session's
           工作目录 — TWO collapsible sections in one SHARED header row (文件树 /
           Git Graph sit side by side, each header toggling its panel). With both
           open the panels stack in a vertical Splitter: drag the gutter to give
           either one more room. The LOGO above stays in every body. -->
      <div
        v-if="sidebarBody === 'files'"
        class="flex min-h-0 flex-1 flex-col">
        <!-- The two section headers always share ONE row: each toggles its panel,
             and a collapsed one keeps its place beside the open one. -->
        <div class="mb-1 flex shrink-0 items-center gap-1">
          <button
            v-for="section in sidebarSections"
            :key="section.id"
            type="button"
            class="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1.5 py-1 text-xs transition-colors"
            :class="
              section.open
                ? 'text-gray-600 dark:text-gray-300'
                : 'text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/60'
            "
            :aria-expanded="section.open"
            :data-test="`sidebar-${section.id}-toggle`"
            @click="section.toggle()">
            <i
              class="pi text-[10px] transition-transform duration-200"
              :class="section.open ? 'pi-chevron-down' : 'pi-chevron-right'" />
            <i :class="[section.icon, 'text-theme-main']" />
            <span class="truncate">{{ t(section.label) }}</span>
          </button>
        </div>

        <!-- Both open: the vertical Splitter, drag its gutter to resize. The
             Splitter is mounted ONLY while both panels exist — its panels must
             never change under a live instance (PrimeVue keeps refs to them and
             throws `Cannot read properties of undefined (reading 'style')` when a
             `v-if` panel disappears), so the single-panel cases render a plain
             container instead of a one-panel splitter. -->
        <Splitter
          v-if="ui.filesSectionOpen && ui.gitSectionOpen"
          layout="vertical"
          class="min-h-0 flex-1"
          data-test="sidebar-splitter"
          @resizeend="onSplitResize">
          <SplitterPanel
            :size="ui.filesSplitSize"
            :min-size="15">
            <div class="flex h-full min-h-0 flex-col">
              <ProjectFileTree
                class="min-h-0 flex-1"
                :session-id="routeSessionId" />
            </div>
          </SplitterPanel>
          <SplitterPanel
            :size="100 - ui.filesSplitSize"
            :min-size="15">
            <div class="flex h-full min-h-0 flex-col">
              <GitGraphPanel
                class="min-h-0 flex-1"
                :session-id="routeSessionId" />
            </div>
          </SplitterPanel>
        </Splitter>
        <div
          v-else-if="ui.filesSectionOpen"
          class="flex min-h-0 flex-1 flex-col"
          data-test="sidebar-files-panel">
          <ProjectFileTree
            class="min-h-0 flex-1"
            :session-id="routeSessionId" />
        </div>
        <div
          v-else-if="ui.gitSectionOpen"
          class="flex min-h-0 flex-1 flex-col"
          data-test="sidebar-git-panel">
          <GitGraphPanel
            class="min-h-0 flex-1"
            :session-id="routeSessionId" />
        </div>
      </div>
      <template v-else>
        <!-- New chat -->
        <Button
          icon="pi pi-comment"
          :label="t('toolbar.newChat')"
          class="mb-3"
          @click="handleCreateSession"
          size="small" />
        <!-- Filter toggle: collapsed by default, no search box shown while collapsed (reuses the ChatBox collapsible block's chevron+rotate pattern) -->
        <div
          class="flex items-center mb-2 cursor-pointer select-none text-xs text-[#868686]"
          role="button"
          tabindex="0"
          :aria-expanded="showSessionFilters"
          @click="showSessionFilters = !showSessionFilters"
          @keydown.enter.prevent="showSessionFilters = !showSessionFilters"
          @keydown.space.prevent="showSessionFilters = !showSessionFilters">
          <span>{{ t('history.filterToggle') }}</span>
          <i
            :class="[
              'pi pi-chevron-down text-xs ml-auto transition-transform duration-200',
              { 'rotate-180': showSessionFilters }
            ]" />
        </div>
        <!-- Filter bar: title keyword + creation date range (local filtering, the two conditions combine with AND, both optional) -->
        <div
          v-if="showSessionFilters"
          class="flex flex-col gap-2 mb-3">
          <InputText
            v-model="searchKeyword"
            class="w-full"
            :placeholder="t('history.searchPlaceholder')" />
          <Calendar
            v-model="dateRange"
            selectionMode="range"
            showIcon
            fluid
            class="w-full"
            :placeholder="t('history.dateRange')" />
          <Button
            v-if="hasActiveFilters"
            icon="pi pi-filter-slash"
            :label="t('history.clearFilter')"
            size="small"
            text
            severity="secondary"
            @click="clearFilters" />
        </div>
        <!-- Records list: windowed (only the visible band of session cards is
             mounted), so a long session history no longer costs a card per entry.
             NOT a flex container: a flex parent shrinks the spacer below its
             declared height and the scroll range collapses to the rendered band. -->
        <div
          ref="sessionsScrollRef"
          class="overflow-auto flex-1">
          <div
            v-if="filteredHistoryList.length === 0"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            {{ hasActiveFilters ? t('history.noSearchResults') : t('history.noSessions') }}
          </div>
          <div
            v-else
            class="relative w-full shrink-0"
            :style="{ height: `${sessionTotalSize}px` }">
            <div
              v-for="vRow in sessionVirtualRows"
              :key="String(vRow.key)"
              :ref="el => sessionVirtualizer.measureElement(el as HTMLElement)"
              :data-index="vRow.index"
              :class="['absolute left-0 top-0 w-full', vRow.index < sessionRows.length - 1 ? 'pb-3' : '']"
              :style="{ transform: `translateY(${vRow.start}px)` }">
              <HistoryItem
                v-if="sessionRowAt(vRow.index)"
                :history-record="sessionRowAt(vRow.index)!.item"
                :is-active="currentSessionId === sessionRowAt(vRow.index)!.item.id"
                @choose-session="handleToggleSession"
                @delete-session="handleDeleteSession"
                @rename-session="handleRenameSession"
                v-model:selectedList="selectedSessionIds" />
            </div>
          </div>
        </div>
        <div class="h-17 flex items-center justify-between">
          <div class="flex items-center justify-center gap-1">
            <Checkbox
              :model-value="isCheckAllSession"
              :indeterminate="isIndeterminate"
              binary
              @update:model-value="handleToggleSelectAll" />
            <span>{{ t('history.selectAll') }}</span>
          </div>
          <Button
            icon="pi pi-trash"
            :label="t('history.batchDelete')"
            :disabled="selectedSessionIds.length === 0 || batchDeleting"
            :loading="batchDeleting"
            @click="handleBatchDelete" />
        </div>
      </template>
    </div>
  </div>
</template>

<script lang="ts">
// Methods/types (regular script block: only for exporting ensureSessionCharacter to parent component for reuse)
import type { CachedCharacter } from '@/composables/db';
import { logUtil } from '~/utils/log';

/**
 * Default character display info (built-in: Touno Hanna / Sherry Orange + default avatar URLs, see `defaultCharacter.ts`).
 * Used as fallback data source for Dexie locking when session hasn't locked character snapshot yet.
 */
const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

/**
 * Ensure the specified session has locked its own character snapshot.
 *
 * Naming logic: System configuration - character configuration edits the 'global pending profile' (`GLOBAL_SESSION_KEY` row).
 * When each session is first opened, copy and lock the current global profile to its own `session_id` row;
 * Subsequent global updates (avatar/name changes) no longer affect old sessions with locked snapshots, only new sessions get the latest global values.
 * Locking result is consumed by [sid].vue through `readCachedCharacter(sessionId)`.
 *
 * Exported for reuse by home/index.vue (load current session snapshot after system config save, initialize default session on first screen).
 *
 * @param sessionId Session ID
 */
export async function ensureSessionCharacter(sessionId: string) {
  try {
    const [globalSnap, sessionSnap] = await Promise.all([
      readCachedCharacter(GLOBAL_SESSION_KEY),
      readCachedCharacter(sessionId)
    ]);
    // Session already has snapshot (old session locked avatar/name) → keep as-is, don't overwrite old session snapshot.
    if (sessionSnap) {
      return;
    }
    // Session has no snapshot yet (new session or never opened before) → use global profile snapshot and lock it.
    // Note: `base` might be the global row (with session_id=GLOBAL_SESSION_KEY),
    // must use `...base` then explicitly override session_id, avoid writing real session key into global row.
    const base = globalSnap ?? defaultCharacter();
    const locked: CachedCharacter = { ...base, session_id: sessionId };
    await cacheCharacter(locked);
  } catch (error) {
    // Don't block chat on Dexie read/write exceptions.
    logUtil.w('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
}
</script>

<script setup lang="ts">
// components
import HistoryItem from './HistoryItem.vue';
import ProjectFileTree from './ProjectFileTree.vue';
import GitGraphPanel from './GitGraphPanel.vue';
// function
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord } from '../type.ts';
// Bridge modules are not auto-imported; the session list asks for its branch
// links the same way it asks for sessions.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { listSideChatLinks } from '~/composables/bridge/session';
// `useVirtualRows` and the row builders come from Nuxt's composable
// auto-import (a value import of `@/composables/**` is lint-restricted).
import { isValidSessionTitle } from '@/common/utils';
import { useNewSessionStore } from '@/stores/new-session';
import { RIGHT_SIDEBAR_MIN_WIDTH, useRightSidebarStore } from '@/stores/right-sidebar';
import { useSidebarWidthStore } from '@/stores/sidebar-width';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { discardSideChatSession } from '~/composables/use-side-chat';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { listSideChatChildren } from '~/composables/bridge/session';

const { t } = useI18n();

/** Mandatory new-session preset dialog (shared with `[sid].vue`'s entry points). */
const newSession = useNewSessionStore();

/**
 * A session was created through the preset dialog (mounted in the shell): show
 * its row here without a reload. Guarded by id — the placeholder also arrives
 * through `loadSessionList` on the next list refresh.
 * @param meta Placeholder entry of the new session.
 */
const onSessionCreated = (meta: unknown) => {
  const row = meta as SessionRecord | null;
  if (!row?.id || historyList.value.some(item => item.id === row.id)) return;
  // A branch/side chat announces its parent: resolve the parent's title here
  // (the list owns it) so the row shows "forked from …" immediately, without
  // waiting for the next list refresh's link merge.
  const parentTitle = row.parentSessionId
    ? historyList.value.find(item => item.id === row.parentSessionId)?.title
    : undefined;
  historyList.value = [{ ...row, parentTitle }, ...historyList.value];
};
on('session:created', onSessionCreated);
onBeforeUnmount(() => off('session:created', onSessionCreated));
const router = useRouter();
const route = useRoute();
const localePath = useLocalePath();

// Background tasks live in the right sidebar's task-detail tab now; the left
// column only lists sessions.

/** Whether collapsed (controlled by parent component via v-model:collapsed, collapse/expand buttons in parent component toolbar) */
const collapsed = defineModel<boolean>('collapsed', { default: false });

/**
 * Extra width added by the shell while the conversation column is floated: the
 * middle column leaves the flex row and its width is shared between the side
 * columns. This is a PRESENTATION override — the stored width below is never
 * written — so docking the chat restores the column exactly.
 */
const props = withDefaults(defineProps<{ extraWidth?: number }>(), { extraWidth: 0 });

// ── Width (draggable, persisted) ───────────────────────────────────────────
/** The shell's left-column width, shared with the layout's auto-collapse. */
const sidebarWidthStore = useSidebarWidthStore();
const sidebarWidth = computed(() => sidebarWidthStore.width);
/** What the DOM actually renders: the stored width plus the float's share. */
const displayWidth = computed(() => sidebarWidth.value + Math.max(0, props.extraWidth));
const rightSidebar = useRightSidebarStore();

/**
 * Pixels the rest of the shell needs at minimum: the conversation's 320px
 * floor plus the right column's floor while it is open. The drag ceiling
 * shrinks by this much, so a wide left column can never push the other two
 * past their own minimums (which would overflow the window instead).
 */
const shellReservedWidth = computed(() => 320 + (rightSidebar.collapsed ? 0 : RIGHT_SIDEBAR_MIN_WIDTH));

/** Whether a pointer drag is in progress (drives the grip's highlighted state). */
const resizing = ref(false);
let resizeStartX = 0;
let resizeStartWidth = 0;

/**
 * Follow the pointer: the column is left-anchored, so moving RIGHT (dx > 0)
 * widens it. `setWidth` clamps each step, so the drag never leaves the band.
 * @param event Pointer move event.
 */
const onResizeMove = (event: PointerEvent) => {
  if (!resizing.value) return;
  sidebarWidthStore.setWidth(
    resizeStartWidth + (event.clientX - resizeStartX),
    window.innerWidth,
    shellReservedWidth.value
  );
};

/** End the drag and detach the window listeners. */
const stopResize = () => {
  if (!resizing.value) return;
  resizing.value = false;
  window.removeEventListener('pointermove', onResizeMove);
  window.removeEventListener('pointerup', stopResize);
  window.removeEventListener('pointercancel', stopResize);
};

/**
 * Start a width drag from the right-edge handle.
 * @param event Pointer down event on the handle.
 */
const startResize = (event: PointerEvent) => {
  resizing.value = true;
  resizeStartX = event.clientX;
  resizeStartWidth = sidebarWidth.value;
  window.addEventListener('pointermove', onResizeMove);
  window.addEventListener('pointerup', stopResize);
  window.addEventListener('pointercancel', stopResize);
  event.preventDefault();
};

/**
 * Keep the column inside its band when the window shrinks: a width saved on a
 * wide window would otherwise overflow a narrower one (mirrors the right
 * sidebar's `fitToViewport`).
 */
const fitWidthToViewport = () => sidebarWidthStore.fitToViewport(window.innerWidth, shellReservedWidth.value);

/** Which body the sidebar shows; the top bar's folder button flips it. */
const ui = useUiStore();

/** The two working-directory sections: their headers share one row, each toggles its panel. */
const sidebarSections = computed<Array<{ id: string; icon: string; label: string; open: boolean; toggle: () => void }>>(
  () => [
    {
      id: 'files',
      icon: 'pi pi-folder',
      label: 'projectFiles.title',
      open: ui.filesSectionOpen,
      toggle: () => ui.toggleFilesSection()
    },
    {
      id: 'git',
      icon: 'pi pi-sitemap',
      label: 'gitGraph.title',
      open: ui.gitSectionOpen,
      toggle: () => ui.toggleGitSection()
    }
  ]
);

/**
 * Remember the split the gutter was dragged to (PrimeVue reports the panel sizes).
 * The Splitter only renders with BOTH sections open, so index 0 is the file tree.
 * @param event The Splitter resizeend event.
 * @param event.sizes Panel shares in per cent, in panel order.
 */
const onSplitResize = (event: { sizes: number[] }): void => {
  if (ui.filesSectionOpen && ui.gitSectionOpen && Array.isArray(event.sizes) && event.sizes[0] !== undefined) {
    ui.setFilesSplitSize(event.sizes[0]);
  }
};
const sidebarBody = computed(() => ui.sidebarBody);
/** The session in view — the tree belongs to that session's project directory. */
const routeSessionId = computed(() => String(route.params.sid ?? ''));

/** Current session id (bidirectionally synced by parent component via v-model:current-session-id, parent uses it to load character snapshot) */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

/** History sessions */
const historyList = ref<SessionRecord[]>([]);

/** Filter bar expand state: collapsed by default, no search box shown when collapsed */
const showSessionFilters = ref(false);

/** Filter: title keyword (empty/blank considered disabled) */
const searchKeyword = ref('');
/** Filter: creation date range (PrimeVue Calendar range mode, null/empty array considered disabled) */
const dateRange = ref<Date[] | null>(null);

/**
 * Filtered session list: keyword and creation date range both effective (AND), pure client-side filtering without requests;
 * When both conditions are disabled, return historyList as-is (same reference, avoid unnecessary array reconstruction).
 */
const filteredHistoryList = computed(() => filterSessions(historyList.value, searchKeyword.value, dateRange.value));

/* ------------------------------------------------------------------ */
/* Windowed session list                                                */
/* ------------------------------------------------------------------ */
/** Sessions list scroll container (only mounted while the sessions tab is active) */
const sessionsScrollRef = useTemplateRef<HTMLDivElement>('sessionsScrollRef');

/** Virtual rows for the (filtered) session list. */
const sessionRows = computed(() => buildSessionRows(filteredHistoryList.value));
const {
  virtualizer: sessionVirtualizer,
  virtualRows: sessionVirtualRows,
  totalSize: sessionTotalSize,
  rowAt: sessionRowAt,
  remeasure: remeasureSessions
} = useVirtualRows(
  sessionsScrollRef,
  () => sessionRows.value,
  () => SESSION_ROW_ESTIMATE_PX
);

// The float widens this column: titles wrap differently, so the virtual rows'
// measured heights change — re-measure once the new width is on screen.
watch(
  () => props.extraWidth,
  () => remeasureSessions()
);

/**
 * Back to the top whenever the session filter changes: the window is derived
 * from the scroll offset, so keeping the old offset after narrowing the list
 * would show an arbitrary slice of the new results.
 */
watch([searchKeyword, dateRange], () => {
  const el = sessionsScrollRef.value;
  if (el) el.scrollTop = 0;
});

/** Whether any filter condition is active (controls 'Clear Filters' button and empty state text) */
const hasActiveFilters = computed(() => {
  if (searchKeyword.value.trim().length > 0) return true;
  return Array.isArray(dateRange.value) && dateRange.value.some(d => d != null);
});

/** Clear filter conditions: reset keyword and date range */
const clearFilters = () => {
  searchKeyword.value = '';
  dateRange.value = null;
};

/** Selected sessions */
const selectedSessionIds = ref<string[]>([]);
/**
 * Select all state: based on 'visible after filtering' sessions — only checked when all visible items are selected;
 * Items filtered out but still in selectedSessionIds don't affect checked state.
 */
const isCheckAllSession = computed(
  () =>
    filteredHistoryList.value.length > 0 &&
    filteredHistoryList.value.every(s => selectedSessionIds.value.includes(s.id))
);
/**
 * Session selection state (indeterminate): based on 'visible after filtering' sessions —
 * indeterminate when only some visible items are selected; not indeterminate when all selected or none selected.
 */
const isIndeterminate = computed(() => {
  const visible = filteredHistoryList.value;
  const selectedVisible = visible.filter(s => selectedSessionIds.value.includes(s.id)).length;
  return selectedVisible > 0 && selectedVisible < visible.length;
});

/**
 * Fetch complete session list from server and populate left-side history list.
 *
 * The only authoritative source for session list is server (context_engine). Here we map server-returned
 * `{session_id, last_time, title}` to frontend `SessionRecord` (id / createTime / title).
 * Locally created but not yet persisted sessions (createTime is local time) will be kept at list top.
 */
const loadSessionList = async () => {
  try {
    // Sessions + their branch links in parallel: the links are one small table
    // read, fetched once per list refresh (never per row).
    const [sessions, branchLinks] = await Promise.all([
      getSessionList(),
      listSideChatLinks().catch(e => {
        logUtil.w('[loadSessionList] 分支关系查询失败：', e);
        return [];
      })
    ]);
    // Merge locally created sessions that don't exist on server yet (IndexedDB placeholders, can still recover after refresh):
    // 1) Read persisted placeholder sessions in IndexedDB (new empty sessions not yet sent messages);
    // 2) Sessions with server records (messages sent) are kept directly from memory list, and their placeholders are cleared;
    // 3) Local items in memory `historyList` (newly created in this session but not yet written to IndexedDB, fallback).
    let localPlaceholders = historyList.value.filter(s => !sessions.some(row => row.id === s.id));
    const serverIds = new Set(sessions.map(row => row.id));
    // For sessions with server records, delete their local placeholders (already promoted to real server sessions).
    const placeholders = await readCachedSessionMetaList();
    for (const p of placeholders) {
      if (serverIds.has(p.id)) {
        clearCachedSessionMeta(p.id);
      }
    }
    // Merge: IndexedDB placeholders (refresh recovery) + memory local items (this session fallback), deduplicated.
    const localById = new Map<string, SessionRecord>();
    for (const p of placeholders) {
      localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
    }
    for (const s of localPlaceholders) {
      if (!localById.has(s.id)) localById.set(s.id, s);
    }
    localPlaceholders = Array.from(localById.values());
    // Placeholder sessions sorted by newest first (createTime descending, string format YYYY-MM-DD HH:mm can be compared lexicographically).
    localPlaceholders.sort((a, b) => (b.createTime < a.createTime ? -1 : 1));
    // After merging, apply custom title overlay: edited session titles are fixed, no longer follow last user message
    const overrides = await readSessionTitleOverrides();
    historyList.value = [...localPlaceholders, ...sessions].map(item =>
      overrides.has(item.id) ? { ...item, title: overrides.get(item.id) ?? item.title, renamed: true } : item
    );
    // Branch marks: a row whose child link exists shows "forked from <parent>"
    // and jumps back to the parent. The parent's TITLE is resolved from this
    // same list (a parent is always a normal session here).
    if (branchLinks.length > 0) {
      const linkByChild = new Map(branchLinks.map(link => [link.childSessionId, link]));
      const titleById = new Map(historyList.value.map(row => [row.id, row.title]));
      historyList.value = historyList.value.map(row => {
        const link = linkByChild.get(row.id);
        if (!link) return row;
        return {
          ...row,
          parentSessionId: link.parentSessionId,
          forkTurnNum: link.forkTurnNum ?? undefined,
          parentTitle: titleById.get(link.parentSessionId)
        };
      });
    }
  } catch (error) {
    // When server unreachable: current session memory state preserved, try to recover persisted placeholder sessions from IndexedDB
    logUtil.w('[loadSessionList] 拉取会话列表失败：', error);
    try {
      const placeholders = await readCachedSessionMetaList();
      const localById = new Map<string, SessionRecord>();
      for (const p of placeholders) {
        localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
      }
      for (const s of historyList.value) {
        if (!localById.has(s.id)) localById.set(s.id, s);
      }
      // Apply custom title overlay: offline session renaming also maintains custom titles
      const overrides = await readSessionTitleOverrides();
      historyList.value = Array.from(localById.values()).map(item =>
        overrides.has(item.id) ? { ...item, title: overrides.get(item.id) ?? item.title, renamed: true } : item
      );
    } catch (cacheErr) {
      logUtil.w('[loadSessionList] 恢复本地占位会话失败：', cacheErr);
    }
  }
};

/**
 * Add new session: the mandatory preset dialog does the work (persona apply +
 * session creation); this button only opens it. The dialog announces the created
 * session over mitt (`session:created`) and this list picks the row up, so the
 * sidebar reflects it without a reload.
 */
const handleCreateSession = () => {
  newSession.openDialog();
};

/**
 * Session switch: route to corresponding session page.
 * [sid].vue is cached by KeepAlive using session_id, switches restore its draft/scroll/streaming state as-is.
 * @param id
 */
const handleToggleSession = (id: string) => {
  if (currentSessionId.value === id) return;
  currentSessionId.value = id;
  // Switch session: load this session's locked character snapshot (use global profile lock if no snapshot)
  ensureSessionCharacter(id);
  router.push(localePath(`/home/${id}`));
};

/**
 * Rename session: takes effect locally immediately (can be searched), and persists overlay.
 * Title overlay is stored separately in Dexie `sessionTitles` table (not cleared when placeholder session is promoted),
 * next loadSessionList will overwrite server-derived title and mark as `renamed` (shows highlighted color).
 * @param id
 * @param title
 */
async function handleRenameSession(id: string, title: string) {
  // In-depth defense: illegal titles (over 30 chars / contain special chars) are ignored directly, normal path already intercepted in HistoryItem before submission
  if (!isValidSessionTitle(title)) return;
  const item = historyList.value.find(s => s.id === id);
  if (!item) return;
  item.title = title;
  item.renamed = true;
  await saveSessionTitleOverride(id, title);
}

/** Set of session ids being deleted (in-flight anti-reentrancy: delete request for same session only sent once) */
const deletingSessionIds = ref<Set<string>>(new Set());
/** Bulk session deletion in progress (in-flight anti-reentrancy) */
const batchDeleting = ref(false);

/**
 * Delete session: call server clearSession, remove from list after success.
 * If deleting the currently active session, route back to home empty state ([sid].vue instance released by KeepAlive).
 * in-flight anti-reentrancy: if same session is triggered again during deletion, directly ignore (rapid clicks only send one DELETE).
 * @param id
 */
const handleDeleteSession = async (id: string) => {
  if (deletingSessionIds.value.has(id)) return;
  deletingSessionIds.value.add(id);
  try {
    // Side chats survive their panels, so collect every child of this parent
    // (closed ones included) before the server cascade removes the links.
    const sideChatChildren = new Set<string>();
    try {
      for (const childSid of await listSideChatChildren(id)) {
        sideChatChildren.add(childSid);
      }
    } catch (error) {
      logUtil.w('[handleDeleteSession] side-chat child lookup failed:', error);
    }
    const rightSidebar = useRightSidebarStore();
    for (const tab of rightSidebar.tabs) {
      if (tab.kind !== 'sideChat' || tab.payload?.parentSessionId !== id) continue;
      if (tab.payload?.sessionId) sideChatChildren.add(tab.payload.sessionId);
    }

    const ok = await clearSession(id);
    if (!ok) {
      logUtil.w('[handleDeleteSession] Failed to delete session, keeping list item:', id);
      return;
    }
    // The server cascaded the parent deletion into its side chats; drop their
    // local caches and close their open tabs (the server data is already gone).
    for (const childSid of sideChatChildren) {
      try {
        await discardSideChatSession(childSid, id, { serverAlreadyPurged: true });
      } catch (error) {
        logUtil.w('[handleDeleteSession] side-chat cache cleanup failed:', error);
      }
      emit(SESSION_ABORT_STREAM_EVENT, childSid);
      rightSidebar.forgetSession(childSid);
      for (const tab of rightSidebar.tabs) {
        if (tab.kind === 'sideChat' && tab.payload?.sessionId === childSid) {
          rightSidebar.closeTab(tab.id);
        }
      }
    }
    rightSidebar.forgetSession(id);
    historyList.value = historyList.value.filter(s => s.id !== id && !sideChatChildren.has(s.id));
    selectedSessionIds.value = selectedSessionIds.value.filter(sid => sid !== id && !sideChatChildren.has(sid));
    // Synchronously clear this session's character snapshot cache
    clearCachedCharacter(id);
    // …and its persona preset binding
    clearCachedSessionPreset(id);
    // Synchronously clear local placeholder session cache (IndexedDB), avoid remaining placeholders after deletion
    clearCachedSessionMeta(id);
    // Synchronously clear custom title overlay (IndexedDB), avoid leaving orphan overlay records after deletion
    await clearSessionTitleOverride(id);
    // This session may still be streaming (especially inactive sessions, their [sid].vue still KeepAlive cached and stream not aborted).
    // Broadcast abort event, let corresponding [sid].vue instance abort its AbortController, avoid stream still pushing chunks in background after deletion, contaminating chat state.
    emit(SESSION_ABORT_STREAM_EVENT, id);
    if (currentSessionId.value === id || (currentSessionId.value && sideChatChildren.has(currentSessionId.value))) {
      currentSessionId.value = undefined;
      router.push(localePath('/home'));
    }
  } catch (error) {
    logUtil.w('[handleDeleteSession] Exception deleting session, keeping list item:', id, error);
  } finally {
    deletingSessionIds.value.delete(id);
  }
};

/**
 * Select all/Deselect all: only affects 'visible after filtering' sessions, original selected state of hidden (filtered out) items remains unchanged
 * @param checked
 */
const handleToggleSelectAll = (checked: boolean) => {
  const visibleIds = new Set(filteredHistoryList.value.map(s => s.id));
  if (checked) {
    // Check all: select all currently visible (filtered) sessions, keep existing selections of hidden items
    selectedSessionIds.value = Array.from(new Set([...selectedSessionIds.value, ...visibleIds]));
  } else {
    // Deselect all: only deselect currently visible items
    selectedSessionIds.value = selectedSessionIds.value.filter(id => !visibleIds.has(id));
  }
};

// PrimeVue confirmation dialog service (ConfirmationService auto-registered by nuxt module, ConfirmDialog mounted in app.vue)
const confirm = useConfirm();

/**
 * Bulk delete sessions: after PrimeVue confirmation dialog confirmation, call server clearSession one by one, uniformly remove from list after success.
 * If current active session is among them, route back to home empty state.
 * in-flight anti-reentrancy: bulk deletion in progress, directly ignore on re-click (button disabled + loading simultaneously).
 */
const handleBatchDelete = () => {
  if (batchDeleting.value) return;
  if (selectedSessionIds.value.length === 0) return;
  confirm.require({
    header: t('common.confirmDelete'),
    message: t('history.batchDeleteConfirm'),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: () => {
      void doBatchDeleteSessions();
    }
  });
};

/** Actual executor for bulk session deletion (triggered by confirmation dialog accept callback). */
const doBatchDeleteSessions = async () => {
  batchDeleting.value = true;
  try {
    const ids = [...selectedSessionIds.value];
    const remain: string[] = [];
    let failed = false;
    for (const id of ids) {
      try {
        const ok = await clearSession(id);
        if (!ok) {
          failed = true;
          remain.push(id);
        }
      } catch (error) {
        failed = true;
        remain.push(id);
        logUtil.w('[handleBatchDelete] Exception deleting session:', id, error);
      }
    }

    const deleted = ids.filter(id => !remain.includes(id));
    if (deleted.length > 0) {
      // A destroyed session's recent-tabs history dies with it.
      const rightSidebar = useRightSidebarStore();
      for (const id of deleted) rightSidebar.forgetSession(id);
      historyList.value = historyList.value.filter(s => !deleted.includes(s.id));
      // Synchronously clear deleted sessions' character snapshot cache
      for (const id of deleted) clearCachedCharacter(id);
      // …and their persona preset bindings
      for (const id of deleted) clearCachedSessionPreset(id);
      // Synchronously clear deleted sessions' custom title overlay (IndexedDB), avoid leaving orphan overlay records
      for (const id of deleted) await clearSessionTitleOverride(id);
      // Deleted sessions may still be streaming (inactive instances in KeepAlive cache with streams not aborted),
      // broadcast abort events one by one, let corresponding [sid].vue instances abort their AbortControllers.
      for (const id of deleted) emit(SESSION_ABORT_STREAM_EVENT, id);
    }
    if (currentSessionId.value && deleted.includes(currentSessionId.value)) {
      currentSessionId.value = undefined;
      router.push(localePath('/home'));
    }
    selectedSessionIds.value = remain;

    if (failed && remain.length > 0) {
      logUtil.w('[handleBatchDelete] Some sessions failed to delete, kept:', remain);
    }
  } finally {
    batchDeleting.value = false;
  }
};

// Load default session character display info (avatar + name) on first screen
ensureSessionCharacter('default');
// After mounting, fetch the session list (WS subscription is a module-level
// singleton, idempotent; character info already loaded by
// ensureSessionCharacter from local Dexie).
onMounted(() => {
  loadSessionList();
  fitWidthToViewport();
  window.addEventListener('resize', fitWidthToViewport);
});

onBeforeUnmount(() => {
  stopResize();
  window.removeEventListener('resize', fitWidthToViewport);
});

// Stronger guarantee: use the session_id at the end of the browser URL as the 'single source of truth' for the active state.
// Use immediate watch on route.params.sid, covering three scenarios simultaneously:
//   1) Refresh/direct access to /home/{sid}: restore highlight immediately on component mount (previously currentSessionId initialized as undefined,
//      without restoring, sidebar would have no active state background);
//   2) In-browser navigation (back/forward/URL change): synchronously move highlight when sid changes, no full page refresh needed;
//   3) Timing race: regardless of loadSessionList return order, as long as URL has sid, always use it as the active item.
const activeSessionId = computed(() => {
  const sid = route.params.sid;
  return typeof sid === 'string' && sid ? sid : undefined;
});
watch(
  activeSessionId,
  async sid => {
    currentSessionId.value = sid;
    if (sid) {
      // Load this session's locked character snapshot (use global profile lock if no snapshot)
      await ensureSessionCharacter(sid);
    }
  },
  { immediate: true }
);
</script>
