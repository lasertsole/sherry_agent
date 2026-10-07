<template>
  <!-- Left side - history records area -->
  <!-- Mobile: fixed positioning, hidden by default, toggled via a button -->
  <!-- md: fixed positioning, width 280px -->
  <!-- lg: relative positioning, width 360px -->
  <div
    :class="[
      'relative h-full overflow-hidden transition-all duration-300',
      collapsed
        ? 'w-0 border-r-0'
        : 'w-[280px] md:w-[280px] lg:w-[360px] border-r border-solid border-gray-light bg-transparent dark:border-gray-dark dark:bg-transparent'
    ]">
    <!-- Fixed content width: when collapsed the outer overflow-hidden clips it wholesale, inner elements are never squeezed or wrapped -->
    <div class="flex flex-col px-4 h-full w-[280px] md:w-[280px] lg:w-[360px]">
      <!-- LOGO area -->
      <div class="flex items-center h-15 text-xl">🍊{{ t('chatBox.defaultAiName') }}</div>
      <!-- Left-sidebar body switch: the session list (default) or the session's
           工作目录 — TWO collapsible sections (file tree + git graph), each with
           its own header that toggles and its own scroller. The LOGO above stays
           in every body. -->
      <div
        v-if="sidebarBody === 'files'"
        class="flex min-h-0 flex-1 flex-col gap-2">
        <section class="flex min-h-0 flex-1 flex-col">
          <button
            type="button"
            class="flex w-full shrink-0 items-center gap-1.5 rounded px-1 py-1 text-xs text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800/60"
            :aria-expanded="ui.filesSectionOpen"
            data-test="sidebar-files-toggle"
            @click="ui.toggleFilesSection()">
            <i
              class="pi pi-chevron-down text-[10px] transition-transform duration-200"
              :class="{ '-rotate-90': !ui.filesSectionOpen }" />
            <i class="pi pi-folder text-theme-main" />
            <span>{{ t('projectFiles.title') }}</span>
          </button>
          <ProjectFileTree
            v-if="ui.filesSectionOpen"
            class="min-h-0 flex-1"
            :session-id="routeSessionId" />
        </section>

        <section class="flex min-h-0 flex-1 flex-col">
          <button
            type="button"
            class="flex w-full shrink-0 items-center gap-1.5 rounded px-1 py-1 text-xs text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800/60"
            :aria-expanded="ui.gitSectionOpen"
            data-test="sidebar-git-toggle"
            @click="ui.toggleGitSection()">
            <i
              class="pi pi-chevron-down text-[10px] transition-transform duration-200"
              :class="{ '-rotate-90': !ui.gitSectionOpen }" />
            <i class="pi pi-sitemap text-theme-main" />
            <span>{{ t('gitGraph.title') }}</span>
          </button>
          <GitGraphPanel
            v-if="ui.gitSectionOpen"
            class="min-h-0 flex-1"
            :session-id="routeSessionId" />
        </section>
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
import { computed, onBeforeUnmount, onMounted, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord } from '../type.ts';
// `useVirtualRows` and the row builders come from Nuxt's composable
// auto-import (a value import of `@/composables/**` is lint-restricted).
import { isValidSessionTitle } from '@/common/utils';
import { useNewSessionStore } from '@/stores/new-session';

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
  historyList.value = [row, ...historyList.value];
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

/** Which body the sidebar shows; the top bar's folder button flips it. */
const ui = useUiStore();
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
  rowAt: sessionRowAt
} = useVirtualRows(
  sessionsScrollRef,
  () => sessionRows.value,
  () => SESSION_ROW_ESTIMATE_PX
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
    const sessions = await getSessionList();
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
    const ok = await clearSession(id);
    if (!ok) {
      logUtil.w('[handleDeleteSession] Failed to delete session, keeping list item:', id);
      return;
    }
    historyList.value = historyList.value.filter(s => s.id !== id);
    selectedSessionIds.value = selectedSessionIds.value.filter(sid => sid !== id);
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
    if (currentSessionId.value === id) {
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

<i18n lang="json">
{
  "zh": {
    "history": {
      "batchDelete": "批量删除对话",
      "batchDeleteConfirm": "确定要批量删除选中的对话吗？此操作不可恢复。",
      "clearFilter": "清除筛选",
      "dateRange": "按日期范围筛选",
      "filterToggle": "筛选",
      "noSearchResults": "没有匹配的会话",
      "searchPlaceholder": "搜索会话标题…",
      "selectAll": "全选"
    },
    "sidebar": {
      "callingSession": "调用会话",
      "endTime": "结束时间",
      "startTime": "开始时间",
      "tabSessions": "会话",
      "tabTasks": "后台任务",
      "taskDelete": "删除任务",
      "taskDeleteConfirm": "确定要删除该任务吗？该任务及其所有子任务将被彻底清空，此操作不可恢复。",
      "tasksBatchDelete": "批量删除任务",
      "tasksBatchDeleteConfirm": "确定要批量删除选中的任务吗？该任务及其所有子任务将被彻底清空，此操作不可恢复。",
      "tasksSelectAll": "全选"
    }
  },
  "en": {
    "history": {
      "batchDelete": "Delete Conversations",
      "batchDeleteConfirm": "Are you sure you want to delete the selected conversations? This action cannot be undone.",
      "clearFilter": "Clear filters",
      "dateRange": "Filter by date range",
      "filterToggle": "Filter",
      "noSearchResults": "No matching sessions",
      "searchPlaceholder": "Search sessions by title…",
      "selectAll": "Select All"
    },
    "sidebar": {
      "callingSession": "Calling Session",
      "endTime": "End Time",
      "startTime": "Start Time",
      "tabSessions": "Chats",
      "tabTasks": "Background Tasks",
      "taskDelete": "Delete task",
      "taskDeleteConfirm": "Delete this task? Its root and all child tasks will be permanently cleared. This cannot be undone.",
      "tasksBatchDelete": "Batch delete tasks",
      "tasksBatchDeleteConfirm": "Delete the selected tasks? Their root and all child tasks will be permanently cleared. This cannot be undone.",
      "tasksSelectAll": "Select All"
    }
  },
  "ja": {
    "history": {
      "batchDelete": "会話を一括削除",
      "batchDeleteConfirm": "選択した会話を一括削除してもよろしいですか？この操作は元に戻せません。",
      "clearFilter": "フィルターをクリア",
      "dateRange": "日付範囲で絞り込み",
      "filterToggle": "絞り込み",
      "noSearchResults": "一致するセッションがありません",
      "searchPlaceholder": "セッションタイトルを検索…",
      "selectAll": "すべて選択"
    },
    "sidebar": {
      "callingSession": "呼び出し中セッション",
      "endTime": "終了時刻",
      "startTime": "開始時刻",
      "tabSessions": "会話",
      "tabTasks": "バックグラウンドタスク",
      "taskDelete": "タスクを削除",
      "taskDeleteConfirm": "このタスクを削除しますか？ルートタスクとすべての子タスクが完全に削除されます。この操作は元に戻せません。",
      "tasksBatchDelete": "タスクを一括削除",
      "tasksBatchDeleteConfirm": "選択したタスクを一括削除しますか？ルートタスクとすべての子タスクが完全に削除されます。この操作は元に戻せません。",
      "tasksSelectAll": "すべて選択"
    }
  },
  "ko": {
    "history": {
      "batchDelete": "대화 일괄 삭제",
      "batchDeleteConfirm": "선택한 대화를 일괄 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.",
      "clearFilter": "필터 지우기",
      "dateRange": "날짜 범위로 필터링",
      "filterToggle": "필터",
      "noSearchResults": "일치하는 세션이 없습니다",
      "searchPlaceholder": "세션 제목 검색…",
      "selectAll": "전체 선택"
    },
    "sidebar": {
      "callingSession": "호출 세션",
      "endTime": "종료 시간",
      "startTime": "시작 시간",
      "tabSessions": "대화",
      "tabTasks": "백그라운드 작업",
      "taskDelete": "작업 삭제",
      "taskDeleteConfirm": "이 작업을 삭제하시겠습니까? 루트 작업과 모든 하위 작업이 완전히 삭제됩니다. 이 작업은 되돌릴 수 없습니다.",
      "tasksBatchDelete": "작업 일괄 삭제",
      "tasksBatchDeleteConfirm": "선택한 작업을 일괄 삭제하시겠습니까? 루트 작업과 모든 하위 작업이 완전히 삭제됩니다. 이 작업은 되돌릴 수 없습니다.",
      "tasksSelectAll": "전체 선택"
    }
  }
}
</i18n>
