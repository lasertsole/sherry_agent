<template>
  <div
    :data-session-id="props.historyRecord.id"
    :class="[
      'p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white',
      'dark:bg-[#2a2a36]/[0.6] dark:border-[#555] md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]',
      { 'text-theme-main bg-[#c1d6e5]! ': props.isActive }
    ]"
    @click="emits('chooseSession', props.historyRecord.id)"
    @contextmenu="onContextMenu"
    role="button"
    tabindex="0"
    @keydown.enter.prevent="emits('chooseSession', props.historyRecord.id)"
    @keydown.space.prevent="emits('chooseSession', props.historyRecord.id)">
    <!-- Title -->
    <div class="flex gap-1 items-center">
      <!-- Session type icon (S5): chat default, team, meeting. -->
      <i
        :class="typeIcon"
        class="shrink-0 text-[10px] text-gray-400"
        :title="typeLabel"
        :aria-label="typeLabel"
        data-test="session-type-icon" />
      <span
        v-if="sessionType === 'team' && (historyRecord.teamName || teamNameFromMeta)"
        class="px-1 py-0.5 rounded text-[10px] bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 shrink-0"
        data-test="session-team-badge"
        >{{ historyRecord.teamName || teamNameFromMeta }}</span
      >
      <span
        v-if="sessionType === 'meeting' && (historyRecord.meetingTopic || meetingIdFromMeta)"
        class="px-1 py-0.5 rounded text-[10px] bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 shrink-0"
        data-test="session-meeting-badge"
        >{{ historyRecord.meetingTopic || meetingIdFromMeta }}</span
      >
      <Checkbox
        size="small"
        v-model="modelList"
        :value="props.historyRecord.id"
        @click.stop />
      <InputText
        v-if="isEditing"
        ref="titleInputRef"
        v-model="draftTitle"
        size="small"
        class="flex-1 min-w-0"
        :maxlength="SESSION_TITLE_MAX_LENGTH"
        :placeholder="t('history.titlePlaceholder')"
        :aria-label="t('history.editTitle')"
        @click.stop
        @keydown.enter.stop.prevent="commitRename"
        @keydown.escape.stop.prevent="cancelRename"
        @blur="commitRename" />
      <div
        v-else
        class="flex-1 min-w-0 truncate"
        :class="props.historyRecord.renamed ? 'text-amber-600 dark:text-amber-400' : ''">
        {{ props.historyRecord?.title || t('history.newSession') }}
      </div>
      <!-- Pinned mark: the row sorts to the top of the list. -->
      <i
        v-if="props.pinned"
        class="pi pi-thumbtack shrink-0 text-[10px] text-theme-main"
        :title="t('history.pinnedLabel')"
        :aria-label="t('history.pinnedLabel')" />
      <!-- Branch mark: this row is a fork of another session. Clicking jumps to
           the parent (the fork's own rows stay untouched). -->
      <button
        v-if="props.historyRecord.parentSessionId"
        type="button"
        class="shrink-0 cursor-pointer text-gray-500 hover:text-blue-500 dark:text-gray-400"
        :aria-label="t('session.forkedFrom', { parent: parentTitle })"
        :title="t('session.forkedFrom', { parent: parentTitle })"
        data-test="session-fork-badge"
        @click.stop="emits('chooseSession', props.historyRecord.parentSessionId!)">
        <i class="pi pi-code-branch text-[10px]"></i>
      </button>
      <!-- Rename entry: pencil icon pinned to the far right of the row; clicking enters inline editing -->
      <button
        v-if="!isEditing"
        type="button"
        class="shrink-0 cursor-pointer hover:text-blue-500"
        :aria-label="t('history.editTitle')"
        :title="t('history.editTitle')"
        @click.stop="startEdit">
        <i class="pi pi-pencil"></i>
      </button>
    </div>
    <!-- Live hint for an invalid title (draft non-empty and invalid while editing; empty title = cancel semantics, no hint) -->
    <div
      v-if="isEditing && !isDraftValid"
      class="mt-1 text-[10px] text-red-500">
      {{ t('history.titleInvalid') }}
    </div>
    <!-- Session ID (session_id) -->
    <div class="truncate mt-1 text-[10px] text-gray-600 dark:text-gray-400">
      {{ props.historyRecord.id }}
    </div>
    <!-- Created time & actions -->
    <div class="flex justify-between mt-3 text-xs">
      <span>{{ t('history.createdAt', { time: formattedCreateTime }) }}</span>
      <button
        type="button"
        class="cursor-pointer text-theme-main hover:text-red-500"
        :aria-label="t('a11y.deleteSession')"
        @click.stop="handleDelete">
        <i class="pi pi-trash"></i>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
// Methods/types
import { computed } from 'vue';
import type { SessionRecord } from '../type';
import { useI18n } from 'vue-i18n';
import { formatCompactTimeString, isValidSessionTitle, SESSION_TITLE_MAX_LENGTH } from '@/common/utils';

const { t } = useI18n();

// PrimeVue confirm dialog service (ConfirmationService auto-registered by the nuxt module; ConfirmDialog mounted in app.vue)
const confirm = useConfirm();

const modelList = defineModel('selectedList', { type: Array, default: () => [] });

interface Props {
  historyRecord: SessionRecord;
  isActive: boolean;
  /** Whether this row is pinned to the top of the session list. */
  pinned?: boolean;
}
const props = defineProps<Props>();

/** The parent's display name for the branch tooltip (id when the row is gone). */
const parentTitle = computed(() => props.historyRecord.parentTitle || props.historyRecord.parentSessionId || '');

/** Session type (S5): absent defaults to chat; metadata inference as fallback. */
const sessionType = computed<'chat' | 'team' | 'meeting'>(() => {
  if (props.historyRecord.type) return props.historyRecord.type;
  const meta = props.historyRecord as SessionRecord & { metadata?: Record<string, unknown> };
  if (meta.metadata?.team_name || meta.metadata?.meeting_id) {
    return meta.metadata.meeting_id ? 'meeting' : 'team';
  }
  return 'chat';
});
const typeIcon = computed(
  () => ({ chat: 'pi pi-comments', team: 'pi pi-users', meeting: 'pi pi-video' })[sessionType.value]
);
const typeLabel = computed(() => t(`session.type.${sessionType.value}`));
const teamNameFromMeta = computed(() => {
  const meta = props.historyRecord as SessionRecord & { metadata?: Record<string, unknown> };
  return typeof meta.metadata?.team_name === 'string' ? meta.metadata.team_name : '';
});
const meetingIdFromMeta = computed(() => {
  const meta = props.historyRecord as SessionRecord & { metadata?: Record<string, unknown> };
  return typeof meta.metadata?.meeting_id === 'string' ? meta.metadata.meeting_id : '';
});

const emits = defineEmits<{
  chooseSession: [id: string];
  deleteSession: [id: string];
  renameSession: [id: string, title: string];
  contextMenu: [event: MouseEvent, id: string];
}>();

/** Render the backend's compact time string (YYYYMMDDHHmmss) using the current locale's date format */
const formattedCreateTime = computed(() => {
  const timeStr = props.historyRecord?.createTime;
  if (!timeStr) return '';
  const format = t('history.dateFormat') || 'YYYY-MM-DD HH:mm';
  return formatCompactTimeString(timeStr, format);
});

/**
 * Right-click the row body: open the parent's one ContextMenu anchored at the
 * pointer. Inputs keep their native menu (the inline title editor needs
 * cut/copy/paste); the checkbox is left alone too.
 * @param event The row's contextmenu event.
 */
function onContextMenu(event: MouseEvent): void {
  const target = event.target as HTMLElement | null;
  if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
  event.preventDefault();
  event.stopPropagation();
  emits('contextMenu', event, props.historyRecord.id);
}

/** Delete session: PrimeVue confirm dialog (replacing native confirm); emits the deleteSession event to the parent on confirmation */
const handleDelete = () => {
  confirm.require({
    header: t('common.confirmDelete'),
    message: t('history.deleteConfirm'),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: () => emits('deleteSession', props.historyRecord.id)
  });
};

/** Inline edit state */
const isEditing = ref(false);
const draftTitle = ref('');
const titleInputRef = ref<{ $el: HTMLInputElement } | null>(null);

/** Enter editing: prefill the current title (empty string if empty), then focus and select all for direct replacement */
async function startEdit() {
  draftTitle.value = props.historyRecord.title || '';
  isEditing.value = true;
  await nextTick();
  titleInputRef.value?.$el?.focus();
  titleInputRef.value?.$el?.select();
}

/** Draft validity (empty = cancel semantics, treated as valid to avoid a false red hint) */
const isDraftValid = computed(() => {
  const next = draftTitle.value.trim();
  return !next || isValidSessionTitle(next);
});

/** Commit: guard first to prevent a double commit from Enter followed by blur; empty title counts as cancel; no-op when unchanged; invalid input toasts an error and abandons the edit */
function commitRename() {
  if (!isEditing.value) return;
  isEditing.value = false;
  const next = draftTitle.value.trim();
  const original = props.historyRecord.title || '';
  if (!next) return;
  if (!isValidSessionTitle(next)) {
    toastError(t('history.titleInvalid'));
    return;
  }
  if (next !== original) emits('renameSession', props.historyRecord.id, next);
}

/** Cancel editing (Esc): same guard, avoiding a false rollback when Esc follows an Enter commit */
function cancelRename() {
  if (!isEditing.value) return;
  isEditing.value = false;
}
</script>
