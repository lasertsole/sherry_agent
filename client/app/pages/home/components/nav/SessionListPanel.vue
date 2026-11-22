<template>
  <div
    class="flex min-h-0 flex-1 flex-col"
    data-test="session-list-panel">
    <!-- New chat -->
    <Button
      icon="pi pi-comment"
      :label="t('toolbar.newChat')"
      class="mb-3"
      @click="handleCreateSession"
      size="small" />
    <!-- Filter toggle -->
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
    <!-- Filter bar: title keyword + creation date range (AND, both optional) -->
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
    <!-- Windowed session list -->
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
            :pinned="pinnedSessions.has(sessionRowAt(vRow.index)!.item.id)"
            @choose-session="handleToggleSession"
            @delete-session="handleDeleteSession"
            @rename-session="handleRenameSession"
            @context-menu="openSessionMenu"
            v-model:selectedList="selectedSessionIds" />
        </div>
      </div>
    </div>
    <div class="h-17 flex items-center justify-between gap-2">
      <div class="flex shrink-0 items-center gap-1">
        <Checkbox
          :model-value="isCheckAllSession"
          :indeterminate="isIndeterminate"
          binary
          @update:model-value="handleToggleSelectAll" />
        <span class="truncate whitespace-nowrap">{{ t('history.selectAll') }}</span>
      </div>
      <Button
        icon="pi pi-trash"
        :label="t('history.batchDelete')"
        class="shrink-0 [&>.p-button-label]:truncate [&>.p-button-label]:whitespace-nowrap"
        :disabled="selectedSessionIds.length === 0 || batchDeleting"
        :loading="batchDeleting"
        v-debounce:click.500="handleBatchDelete" />
    </div>
    <!-- One menu for the whole list: rows emit their right-click upward. -->
    <ContextMenu
      ref="sessionMenuRef"
      :model="sessionMenuItems"
      data-test="session-context-menu" />
  </div>
</template>

<script setup lang="ts">
import HistoryItem from '../HistoryItem.vue';
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord } from '../../type.ts';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { listSideChatLinks, listSideChatChildren } from '~/composables/bridge/session';
import { isValidSessionTitle } from '@/common/utils';
import { useNewSessionStore } from '@/stores/new-session';
import { useRightSidebarStore } from '@/stores/right-sidebar';
import { usePinnedSessionsStore } from '@/stores/pinned-sessions';
import ContextMenu from 'primevue/contextmenu';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { discardSideChatSession } from '~/composables/use-side-chat';
import { logUtil } from '~/utils/log';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { SESSION_ABORT_STREAM_EVENT } from '@/composables/messages';

import { ensureSessionCharacter } from '../session-character';

const { t } = useI18n();

/** Session id shown in the main column; synced with the shell/parent. */
const currentSessionId = defineModel<string | undefined>('currentSessionId');
/** Extra width added by the floated chat column (affects virtual row heights). */
const props = withDefaults(defineProps<{ extraWidth?: number }>(), { extraWidth: 0 });

const newSession = useNewSessionStore();
const router = useRouter();
const route = useRoute();
const localePath = useLocalePath();
const pinnedSessions = usePinnedSessionsStore();

const onSessionCreated = (meta: unknown) => {
  const row = meta as SessionRecord | null;
  if (!row?.id || historyList.value.some(item => item.id === row.id)) return;
  const parentTitle = row.parentSessionId
    ? historyList.value.find(item => item.id === row.parentSessionId)?.title
    : undefined;
  historyList.value = [{ ...row, parentTitle }, ...historyList.value];
};
on('session:created', onSessionCreated);
onBeforeUnmount(() => off('session:created', onSessionCreated));

const historyList = ref<SessionRecord[]>([]);
const sessionMenuRef = useTemplateRef<{ show: (event: Event) => void }>('sessionMenuRef');
const sessionMenuItems = ref<Array<{ label: string; icon: string; command: () => void }>>([]);

function openSessionMenu(event: MouseEvent, id: string): void {
  sessionMenuItems.value = [
    {
      label: pinnedSessions.has(id) ? t('history.unpin') : t('history.pin'),
      icon: 'pi pi-thumbtack',
      command: () => pinnedSessions.toggle(id)
    }
  ];
  sessionMenuRef.value?.show(event);
}

const showSessionFilters = ref(false);
const searchKeyword = ref('');
const dateRange = ref<Date[] | null>(null);

const filteredHistoryList = computed(() =>
  sortPinnedFirst(filterSessions(historyList.value, searchKeyword.value, dateRange.value), pinnedSessions.ids)
);

const sessionsScrollRef = useTemplateRef<HTMLDivElement>('sessionsScrollRef');
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

watch(
  () => props.extraWidth,
  () => remeasureSessions()
);
watch([searchKeyword, dateRange], () => {
  const el = sessionsScrollRef.value;
  if (el) el.scrollTop = 0;
});

const hasActiveFilters = computed(() => {
  if (searchKeyword.value.trim().length > 0) return true;
  return Array.isArray(dateRange.value) && dateRange.value.some(d => d != null);
});
const clearFilters = () => {
  searchKeyword.value = '';
  dateRange.value = null;
};

const selectedSessionIds = ref<string[]>([]);
const isCheckAllSession = computed(
  () =>
    filteredHistoryList.value.length > 0 &&
    filteredHistoryList.value.every(s => selectedSessionIds.value.includes(s.id))
);
const isIndeterminate = computed(() => {
  const visible = filteredHistoryList.value;
  const selectedVisible = visible.filter(s => selectedSessionIds.value.includes(s.id)).length;
  return selectedVisible > 0 && selectedVisible < visible.length;
});

const loadSessionList = async () => {
  try {
    const [sessions, branchLinks] = await Promise.all([
      getSessionList(),
      listSideChatLinks().catch(e => {
        logUtil.w('[loadSessionList] 分支关系查询失败：', e);
        return [];
      })
    ]);
    let localPlaceholders = historyList.value.filter(s => !sessions.some(row => row.id === s.id));
    const serverIds = new Set(sessions.map(row => row.id));
    const placeholders = await readCachedSessionMetaList();
    for (const p of placeholders) {
      if (serverIds.has(p.id)) clearCachedSessionMeta(p.id);
    }
    const localById = new Map<string, SessionRecord>();
    for (const p of placeholders) {
      localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
    }
    for (const s of localPlaceholders) {
      if (!localById.has(s.id)) localById.set(s.id, s);
    }
    localPlaceholders = Array.from(localById.values());
    localPlaceholders.sort((a, b) => (b.createTime < a.createTime ? -1 : 1));
    const overrides = await readSessionTitleOverrides();
    historyList.value = [...localPlaceholders, ...sessions].map(item =>
      overrides.has(item.id) ? { ...item, title: overrides.get(item.id) ?? item.title, renamed: true } : item
    );
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
    logUtil.w('[loadSessionList] 拉取会话列表失败：', error);
    try {
      const placeholders = await readCachedSessionMetaList();
      const localById = new Map<string, SessionRecord>();
      for (const p of placeholders) localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
      for (const s of historyList.value) if (!localById.has(s.id)) localById.set(s.id, s);
      const overrides = await readSessionTitleOverrides();
      historyList.value = Array.from(localById.values()).map(item =>
        overrides.has(item.id) ? { ...item, title: overrides.get(item.id) ?? item.title, renamed: true } : item
      );
    } catch (cacheErr) {
      logUtil.w('[loadSessionList] 恢复本地占位会话失败：', cacheErr);
    }
  }
};

const handleCreateSession = () => newSession.openDialog();

const handleToggleSession = (id: string) => {
  const row = historyList.value.find(item => item.id === id);
  const metadata = (row as (SessionRecord & { metadata?: Record<string, unknown> }) | undefined)?.metadata;
  // Meeting sessions route to the meeting page (S5); team/chat stay in chat.
  if (row?.type === 'meeting' || metadata?.meeting_id) {
    const meetingId = row?.meetingId ?? (typeof metadata?.meeting_id === 'string' ? metadata.meeting_id : id);
    router.push(localePath(`/meeting/${meetingId}`));
    return;
  }
  if (currentSessionId.value === id) return;
  currentSessionId.value = id;
  ensureSessionCharacter(id);
  router.push(localePath(`/home/${id}`));
};

async function handleRenameSession(id: string, title: string) {
  if (!isValidSessionTitle(title)) return;
  const item = historyList.value.find(s => s.id === id);
  if (!item) return;
  item.title = title;
  item.renamed = true;
  await saveSessionTitleOverride(id, title);
}

const deletingSessionIds = ref<Set<string>>(new Set());
const batchDeleting = ref(false);

const handleDeleteSession = async (id: string) => {
  if (deletingSessionIds.value.has(id)) return;
  deletingSessionIds.value.add(id);
  try {
    const sideChatChildren = new Set<string>();
    try {
      for (const childSid of await listSideChatChildren(id)) sideChatChildren.add(childSid);
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
    for (const childSid of sideChatChildren) {
      try {
        await discardSideChatSession(childSid, id, { serverAlreadyPurged: true });
      } catch (error) {
        logUtil.w('[handleDeleteSession] side-chat cache cleanup failed:', error);
      }
      emit(SESSION_ABORT_STREAM_EVENT, childSid);
      rightSidebar.forgetSession(childSid);
      for (const tab of rightSidebar.tabs) {
        if (tab.kind === 'sideChat' && tab.payload?.sessionId === childSid) rightSidebar.closeTab(tab.id);
      }
    }
    rightSidebar.forgetSession(id);
    pinnedSessions.remove(id);
    for (const childSid of sideChatChildren) pinnedSessions.remove(childSid);
    historyList.value = historyList.value.filter(s => s.id !== id && !sideChatChildren.has(s.id));
    selectedSessionIds.value = selectedSessionIds.value.filter(sid => sid !== id && !sideChatChildren.has(sid));
    clearCachedCharacter(id);
    clearCachedSessionPreset(id);
    clearCachedSessionMeta(id);
    await clearSessionTitleOverride(id);
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

const handleToggleSelectAll = (checked: boolean) => {
  const visibleIds = new Set(filteredHistoryList.value.map(s => s.id));
  if (checked) {
    selectedSessionIds.value = Array.from(new Set([...selectedSessionIds.value, ...visibleIds]));
  } else {
    selectedSessionIds.value = selectedSessionIds.value.filter(id => !visibleIds.has(id));
  }
};

const confirm = useConfirm();
const handleBatchDelete = () => {
  if (batchDeleting.value || selectedSessionIds.value.length === 0) return;
  confirm.require({
    header: t('common.confirmDelete'),
    message: t('history.batchDeleteConfirm'),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: () => void doBatchDeleteSessions()
  });
};
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
      const rightSidebar = useRightSidebarStore();
      for (const id of deleted) {
        rightSidebar.forgetSession(id);
        pinnedSessions.remove(id);
      }
      historyList.value = historyList.value.filter(s => !deleted.includes(s.id));
      for (const id of deleted) clearCachedCharacter(id);
      for (const id of deleted) clearCachedSessionPreset(id);
      for (const id of deleted) await clearSessionTitleOverride(id);
      for (const id of deleted) emit(SESSION_ABORT_STREAM_EVENT, id);
    }
    if (currentSessionId.value && deleted.includes(currentSessionId.value)) {
      currentSessionId.value = undefined;
      router.push(localePath('/home'));
    }
    selectedSessionIds.value = remain;
    if (failed && remain.length > 0) logUtil.w('[handleBatchDelete] Some sessions failed to delete, kept:', remain);
  } finally {
    batchDeleting.value = false;
  }
};

ensureSessionCharacter('default');
onMounted(() => loadSessionList());

const activeSessionId = computed(() => {
  const sid = route.params.sid;
  return typeof sid === 'string' && sid ? sid : undefined;
});
watch(
  activeSessionId,
  async sid => {
    currentSessionId.value = sid;
    if (sid) await ensureSessionCharacter(sid);
  },
  { immediate: true }
);
</script>
