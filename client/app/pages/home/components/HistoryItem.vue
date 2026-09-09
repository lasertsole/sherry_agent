<template>
  <div
    :class="[
      'p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white',
      'dark:bg-[#2a2a36]/[0.6] dark:border-[#555] md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]',
      { 'text-theme-main bg-[#c1d6e5]! ': props.isActive }
    ]"
    @click="emits('chooseSession', props.historyRecord.id)"
    role="button"
    tabindex="0"
    @keydown.enter.prevent="emits('chooseSession', props.historyRecord.id)"
    @keydown.space.prevent="emits('chooseSession', props.historyRecord.id)">
    <!-- Title -->
    <div class="flex gap-1 items-center">
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
}
const props = defineProps<Props>();

const emits = defineEmits<{
  chooseSession: [id: string];
  deleteSession: [id: string];
  renameSession: [id: string, title: string];
}>();

/** Render the backend's compact time string (YYYYMMDDHHmmss) using the current locale's date format */
const formattedCreateTime = computed(() => {
  const timeStr = props.historyRecord?.createTime;
  if (!timeStr) return '';
  const format = t('history.dateFormat') || 'YYYY-MM-DD HH:mm';
  return formatCompactTimeString(timeStr, format);
});

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

<i18n lang="json">
{
  "zh": {
    "a11y": {
      "deleteSession": "删除会话"
    },
    "history": {
      "createdAt": "创建时间：{time}",
      "dateFormat": "YYYY-MM-DD HH:mm",
      "deleteConfirm": "确定要删除该会话吗？此操作不可恢复。",
      "editTitle": "重命名会话",
      "titleInvalid": "标题限 30 字以内，且只能包含中英文、数字、空格和 . _ -",
      "titlePlaceholder": "输入新标题"
    }
  },
  "en": {
    "a11y": {
      "deleteSession": "Delete session"
    },
    "history": {
      "createdAt": "Created: {time}",
      "dateFormat": "MM/DD/YYYY HH:mm",
      "deleteConfirm": "Are you sure you want to delete this conversation? This action cannot be undone.",
      "editTitle": "Rename session",
      "titleInvalid": "Title must be at most 30 characters and contain only letters, numbers, spaces, and . _ -",
      "titlePlaceholder": "Enter new title"
    }
  },
  "ja": {
    "a11y": {
      "deleteSession": "セッションを削除"
    },
    "history": {
      "createdAt": "作成日時：{time}",
      "dateFormat": "YYYY/MM/DD HH:mm",
      "deleteConfirm": "この会話を削除してもよろしいですか？この操作は元に戻せません。",
      "editTitle": "セッション名を変更",
      "titleInvalid": "タイトルは30文字以内で、英字・数字・空白および . _ - のみ使用できます",
      "titlePlaceholder": "新しいタイトルを入力"
    }
  },
  "ko": {
    "a11y": {
      "deleteSession": "세션 삭제"
    },
    "history": {
      "createdAt": "만든 시간：{time}",
      "dateFormat": "YYYY/MM/DD HH:mm",
      "deleteConfirm": "이 대화를 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.",
      "editTitle": "세션 이름 바꾸기",
      "titleInvalid": "제목은 30자 이내이며 영문·한글·숫자·공백 및 . _ -만 사용할 수 있습니다",
      "titlePlaceholder": "새 제목 입력"
    }
  }
}
</i18n>
