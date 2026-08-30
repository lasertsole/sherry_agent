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
      <!-- Tab switcher: sessions / background tasks -->
      <div class="flex gap-1 my-3 rounded-lg p-1 bg-gray-100 dark:bg-gray-800">
        <button
          class="flex-1 h-8 rounded-md text-sm transition-all cursor-pointer"
          :class="
            activeTab === 'sessions'
              ? 'bg-white dark:bg-gray-700 text-primary font-medium shadow-sm'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
          "
          @click="switchTab('sessions')">
          {{ t('sidebar.tabSessions') }}
        </button>
        <button
          class="flex-1 h-8 rounded-md text-sm transition-all cursor-pointer flex items-center justify-center gap-1"
          :class="
            activeTab === 'tasks'
              ? 'bg-white dark:bg-gray-700 text-primary font-medium shadow-sm'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
          "
          @click="switchTab('tasks')">
          {{ t('sidebar.tabTasks') }}
          <span
            v-if="allRunningTaskCount > 0"
            class="inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-full text-[11px] leading-none text-white bg-red-500">
            {{ allRunningTaskCount }}
          </span>
        </button>
      </div>

      <!-- ===== Sessions Tab ===== -->
      <template v-if="activeTab === 'sessions'">
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
        <!-- Records list -->
        <div class="flex flex-col overflow-auto flex-1 gap-3">
          <div
            v-if="filteredHistoryList.length === 0"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            {{ hasActiveFilters ? t('history.noSearchResults') : t('history.noSessions') }}
          </div>
          <HistoryItem
            v-for="item in filteredHistoryList"
            :key="item.id"
            :history-record="item"
            :is-active="currentSessionId === item.id"
            @choose-session="handleToggleSession"
            @delete-session="handleDeleteSession"
            @rename-session="handleRenameSession"
            v-model:selectedList="selectedSessionIds" />
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

      <!-- ===== Background Tasks Tab ===== -->
      <template v-else>
        <div class="flex flex-col overflow-auto flex-1 gap-2">
          <div
            v-if="taskLoading"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            <i class="pi pi-spin pi-spinner mr-2" />{{ t('sidebar.tasksLoading') }}
          </div>
          <div
            v-else-if="rootTaskRuns.length === 0"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            {{ t('sidebar.noTasks') }}
          </div>
          <template
            v-else
            v-for="group in groupedRootTaskRuns"
            :key="group.sessionId">
            <div
              class="flex items-center gap-2 pt-1.5 pb-0.5 text-[11px] font-semibold uppercase tracking-wide text-[#868686]">
              <span class="flex-none text-[#b0b0b0]">{{ t('sidebar.callingSession') }}:</span>
              <span class="truncate break-all">{{ group.sessionId }}</span>
              <span class="ml-auto flex-none text-[#868686]">({{ group.runs.length }})</span>
            </div>
            <div
              v-for="run in group.runs"
              :key="run.run_id"
              class="p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white dark:bg-[#2a2a36]/[0.6] dark:border-[#555] flex flex-col gap-1.5 md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]"
              :class="{ 'text-theme-main bg-[#c1d6e5]!': focusedRunId === run.run_id }"
              role="button"
              tabindex="0"
              @click="showTasksView(run)"
              @keydown.enter.prevent="showTasksView(run)"
              @keydown.space.prevent="showTasksView(run)">
              <div class="flex items-center gap-2">
                <Checkbox
                  :model-value="selectedRunIds.has(run.run_id)"
                  binary
                  class="flex-none"
                  @update:model-value="handleToggleTask(run.run_id)"
                  @click.stop />
                <span
                  v-if="statusLabel(run) !== t('sidebar.statusUnknown')"
                  class="ml-auto flex-none inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full leading-none"
                  :class="badgeClass(run)">
                  <i
                    v-if="isRunning(run)"
                    class="pi pi-spin pi-spinner text-[10px]" />
                  {{ statusLabel(run) }}
                </span>
              </div>
              <div class="text-[13px] leading-snug line-clamp-2 break-words">
                {{ run.label || run.task_name || '-' }}
              </div>
              <div class="flex justify-between items-center gap-2 text-[11px] leading-snug text-[#868686] break-all">
                <div class="min-w-0">
                  <span class="text-[#b0b0b0]">{{ t('sidebar.startTime') }}: </span
                  >{{ formatTime(run.execution.started_at) }}
                  <span class="mx-1.5 text-[#b0b0b0]">/</span>
                  <span class="text-[#b0b0b0]">{{ t('sidebar.endTime') }}: </span
                  >{{ formatTime(run.execution.ended_at) }}
                </div>
                <!-- Single delete: trash icon (reuses the session box pattern), deletes this task and its entire subtree -->
                <button
                  type="button"
                  class="shrink-0 cursor-pointer text-theme-main hover:text-red-500"
                  :aria-label="t('sidebar.taskDelete')"
                  :title="t('sidebar.taskDelete')"
                  @click.stop="handleDeleteTask(run)">
                  <i
                    class="pi"
                    :class="deletingRunIds.has(run.run_id) ? 'pi-spin pi-spinner' : 'pi-trash'" />
                </button>
              </div>
            </div>
          </template>
        </div>
        <div
          v-if="rootTaskRuns.length > 0"
          class="h-17 flex items-center justify-between">
          <div class="flex items-center justify-center gap-1">
            <Checkbox
              :model-value="allSelected"
              :indeterminate="someSelected"
              binary
              @update:model-value="toggleSelectAllTasks()" />
            <span>{{ t('sidebar.tasksSelectAll') }}</span>
          </div>
          <Button
            icon="pi pi-trash"
            :label="t('sidebar.tasksBatchDelete')"
            :disabled="selectedRunIds.size === 0 || deletingRunIds.size > 0"
            :loading="deletingRunIds.size > 0"
            @click="handleBatchDeleteTasks" />
        </div>
      </template>
    </div>
  </div>
</template>

<script lang="ts">
// Methods/types (regular script block: only for exporting ensureSessionCharacter to parent component for reuse)
import type { CachedCharacter } from '@/composables/db';
import { GLOBAL_SESSION_KEY, DEFAULT_CACHED_CHARACTER, cacheCharacter, readCachedCharacter } from '@/composables/db';

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
    console.warn('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
}
</script>

<script setup lang="ts">
// components
import HistoryItem from './HistoryItem.vue';
// function
import { computed, onMounted, onUnmounted, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord } from '../type.ts';
import {
  clearCachedCharacter,
  cacheSessionMeta,
  readCachedSessionMetaList,
  clearCachedSessionMeta,
  saveSessionTitleOverride,
  readSessionTitleOverrides,
  clearSessionTitleOverride
} from '@/composables/db';
import { emit, on, off } from '@/composables/mitt';
import { getSessionList, clearSession, SESSION_ABORT_STREAM_EVENT } from '@/composables/messages';
import type { SubagentRun } from '@/composables/bridge';
import { useSubagentTasks } from '@/composables/useSubagentTasks';
import dayjs from 'dayjs';
import { filterSessions } from '@/composables/sessionFilter';
import { isValidSessionTitle } from '@/common/utils';

const { t } = useI18n();
const router = useRouter();
const route = useRoute();
const localePath = useLocalePath();

// Background task shared state (module-level singleton, shares same reactive data with right-side complete task list page)
const {
  rootTaskRuns,
  groupedRootTaskRuns,
  taskLoading,
  allRunningTaskCount,
  selectedRunIds,
  deletingRunIds,
  allSelected,
  someSelected,
  isRunning,
  badgeClass,
  statusLabel,
  initTasks,
  setTasksTabActive,
  focusRun,
  focusedRunId,
  loadTaskRuns,
  toggleTaskSelection,
  toggleSelectAllTasks,
  deleteSubagentSubtree,
  deleteSelectedTasks
} = useSubagentTasks();

/** Whether collapsed (controlled by parent component via v-model:collapsed, collapse/expand buttons in parent component toolbar) */
const collapsed = defineModel<boolean>('collapsed', { default: false });

/** Current session id (bidirectionally synced by parent component via v-model:current-session-id, parent uses it to load character snapshot) */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

/** Render execution time: epoch milliseconds → local readable string; null/invalid values show placeholder '-' */
function formatTime(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(Number(ms))) return '-';
  return dayjs(Number(ms)).format('YYYY-MM-DD HH:mm:ss');
}

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
    console.warn('[loadSessionList] 拉取会话列表失败：', error);
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
      console.warn('[loadSessionList] 恢复本地占位会话失败：', cacheErr);
    }
  }
};

/** Add new session: generate random session_id, add to list and route to new session page (KeepAlive caches by sid) */
const handleCreateSession = () => {
  const sessionId = crypto.randomUUID();
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const newSession: SessionRecord = {
    id: sessionId,
    title: t('history.newSession'),
    createTime
  };
  historyList.value = [newSession, ...historyList.value];
  currentSessionId.value = sessionId;
  // New session: immediately create and lock character snapshot with current global profile, ensure avatar/name display correctly
  ensureSessionCharacter(sessionId);
  // Switch back to 'chat' display state: notify right-side [sid].vue to restore chat area
  emit('subagent:show-chat');
  setTasksTabActive(false);
  // Persist placeholder session (written to IndexedDB on creation), ensure this empty session remains in list after refresh/reopen
  // (server session list is derived from message table, no records before messages sent, can only recover from local placeholders).
  cacheSessionMeta({ id: sessionId, title: t('history.newSession'), createTime, updatedAt: Date.now() });
  router.push(localePath(`/home/${sessionId}`));
};

/**
 * Session switch: route to corresponding session page.
 * [sid].vue is cached by KeepAlive using session_id, switches restore its draft/scroll/streaming state as-is.
 */
const handleToggleSession = (id: string) => {
  if (currentSessionId.value === id) return;
  currentSessionId.value = id;
  // Switch session: load this session's locked character snapshot (use global profile lock if no snapshot)
  ensureSessionCharacter(id);
  // Switch back to 'chat' display state: notify right-side [sid].vue to restore chat area
  emit('subagent:show-chat');
  setTasksTabActive(false);
  router.push(localePath(`/home/${id}`));
};

/**
 * Rename session: takes effect locally immediately (can be searched), and persists overlay.
 * Title overlay is stored separately in Dexie `sessionTitles` table (not cleared when placeholder session is promoted),
 * next loadSessionList will overwrite server-derived title and mark as `renamed` (shows highlighted color).
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
 */
const handleDeleteSession = async (id: string) => {
  if (deletingSessionIds.value.has(id)) return;
  deletingSessionIds.value.add(id);
  try {
    const ok = await clearSession(id);
    if (!ok) {
      console.warn('[handleDeleteSession] Failed to delete session, keeping list item:', id);
      return;
    }
    historyList.value = historyList.value.filter(s => s.id !== id);
    selectedSessionIds.value = selectedSessionIds.value.filter(sid => sid !== id);
    // Synchronously clear this session's character snapshot cache
    clearCachedCharacter(id);
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
    console.warn('[handleDeleteSession] Exception deleting session, keeping list item:', id, error);
  } finally {
    deletingSessionIds.value.delete(id);
  }
};

/** Select all/Deselect all: only affects 'visible after filtering' sessions, original selected state of hidden (filtered out) items remains unchanged */
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
        console.warn('[handleBatchDelete] Exception deleting session:', id, error);
      }
    }

    const deleted = ids.filter(id => !remain.includes(id));
    if (deleted.length > 0) {
      historyList.value = historyList.value.filter(s => !deleted.includes(s.id));
      // Synchronously clear deleted sessions' character snapshot cache
      for (const id of deleted) clearCachedCharacter(id);
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
      console.warn('[handleBatchDelete] Some sessions failed to delete, kept:', remain);
    }
  } finally {
    batchDeleting.value = false;
  }
};

// Load default session character display info (avatar + name) on first screen
ensureSessionCharacter('default');
// After mounting, fetch session list + initialize background tasks (WS subscription is module-level singleton, idempotent; character info already loaded by ensureSessionCharacter from local Dexie)
// When receiving 'show chat' event (new session/switch session/background task 'return to session'),
// switch back to 'sessions' tab, ensure session list is visible and highlight target session box.
const onShowChatSwitchTab = () => switchTab('sessions');
onMounted(() => {
  loadSessionList();
  initTasks(activeSessionId.value);
  on('subagent:show-chat', onShowChatSwitchTab);
});
onUnmounted(() => {
  off('subagent:show-chat', onShowChatSwitchTab);
});

/* ------------------------------------------------------------------ */
/* Background Tasks Tab (Subagent Run Records)                         */
/* ------------------------------------------------------------------ */
/** Sidebar current active tab: 'sessions' (sessions) | 'tasks' (background tasks) */
const activeTab = ref<'sessions' | 'tasks'>('sessions');

/**
 * Switch tab (only switches sidebar left-side display list + background tasks loading state, **does not** switch right-side view).
 * Right-side view only switches when clicking specific 'Session Box' (handleToggleSession / handleCreateSession) or
 * 'Background Tasks Box' (showTasksView).
 * - Switch to 'background tasks': mark background tasks in display state, let WS pull full task data for list display when ready.
 * - Switch to 'sessions': unmark that state.
 */
const switchTab = (tab: 'sessions' | 'tasks') => {
  activeTab.value = tab;
  if (tab === 'tasks') {
    setTasksTabActive(true);
    void loadTaskRuns();
  } else {
    setTasksTabActive(false);
  }
};

/**
 * Click task item: switch to 'background tasks' display state, and locate/expand/highlight that run.
 * When there's an active session (route with sid), emit subagent:show-tasks event, received by [sid].vue embedded view and set to task display state;
 * When there's no active session (root path /home, [sid].vue not mounted, event has no receiver), directly focus that run (module-level singleton state preserved across routes)
 * and navigate to standalone task page /home/tasks/{parent session} — that page always mounts SubagentTasksView, can read focused run from singleton state.
 */
const showTasksView = (run: SubagentRun) => {
  activeTab.value = 'tasks';
  // Record currently focused/opened run, used for sidebar task box active state highlight (consistent with session list items)
  focusRun(run.run_id);
  const sid = route.params.sid;
  if (typeof sid === 'string' && sid) {
    // With active session: go through embedded view event flow (by [sid].vue's onShowTasks switching viewMode to 'tasks')
    emit('subagent:show-tasks', run.run_id);
    setTasksTabActive(true);
  } else {
    // Without active session: focus + navigate to standalone task page (parent session of cross-session task tree)
    const parentSid = run.requester_session_key;
    router.push(localePath(`/home/tasks/${parentSid || 'default'}`));
  }
};

/**
 * Toggle single task selection state (only triggered by checkbox within task card).
 * Card body click changed to showTasksView (opens task detail page), avoiding blocking open logic.
 */
const handleToggleTask = (runId: string) => {
  if (deletingRunIds.value.has(runId)) return;
  toggleTaskSelection(runId);
};

/** Batch delete currently selected tasks: PrimeVue confirmation dialog (each task along with its entire subtree completely cleared from frontend and backend cache). */
const handleBatchDeleteTasks = () => {
  if (selectedRunIds.value.size === 0) return;
  confirm.require({
    header: t('common.confirmDelete'),
    message: t('sidebar.tasksBatchDeleteConfirm'),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: () => {
      void doBatchDeleteTasks();
    }
  });
};

/** Actual executor for batch background task deletion (triggered by confirmation dialog accept callback). */
const doBatchDeleteTasks = async () => {
  try {
    const removed = await deleteSelectedTasks();
    if (removed > 0) emit('subagent:refresh-tasks');
  } catch (error) {
    console.error('[SessionSidebar] Failed to batch delete background tasks:', error);
  }
};

/**
 * Single task box deletion (trash icon at bottom right, consistent with session box delete entry).
 * Reuses the same deletion pipeline as batch delete: after PrimeVue confirmation dialog, completely deletes the task and its entire subtree (frontend/backend + Dexie).
 */
const handleDeleteTask = (run: { run_id: string }) => {
  if (deletingRunIds.value.has(run.run_id)) return;
  confirm.require({
    header: t('common.confirmDelete'),
    message: t('sidebar.taskDeleteConfirm'),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: () => {
      void doDeleteTask(run.run_id);
    }
  });
};

/** Actual executor for single background task deletion (triggered by confirmation dialog accept callback). */
const doDeleteTask = async (runId: string) => {
  try {
    await deleteSubagentSubtree(runId);
    emit('subagent:refresh-tasks');
  } catch (error) {
    console.error('[SessionSidebar] Failed to delete background task:', error);
  }
};

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

// Refresh background tasks when switching active session (only if user has opened this Tab before)
watch(
  activeSessionId,
  () => {
    if (activeTab.value === 'tasks') loadTaskRuns();
  },
  { immediate: false }
);
</script>
