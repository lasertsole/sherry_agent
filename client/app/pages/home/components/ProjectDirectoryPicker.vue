<template>
  <!-- In-app folder dialog for the browser build (the desktop build uses the OS
       picker instead). It lists one level at a time through `GET /system/dirs`
       — directories only — and binds the folder you are STANDING IN, the same
       "navigate, then choose the current folder" shape as the system dialog. -->
  <Dialog
    v-model:visible="visible"
    :header="t('toolbar.projectDirectory.pickerTitle')"
    :modal="true"
    class="w-[min(94vw,34rem)]">
    <div class="flex flex-col gap-3">
      <div class="flex items-center gap-2">
        <Button
          icon="pi pi-arrow-up"
          size="small"
          text
          severity="secondary"
          :disabled="!level?.parent || loading"
          :title="t('toolbar.projectDirectory.pickerUp')"
          :aria-label="t('toolbar.projectDirectory.pickerUp')"
          data-test="dir-picker-up"
          @click="goUp" />
        <span
          class="min-w-0 truncate font-mono text-xs text-gray-600 dark:text-gray-300"
          :title="currentPath"
          data-test="dir-picker-path">
          {{ currentPath || t('toolbar.projectDirectory.pickerHome') }}
        </span>
      </div>

      <div class="h-64 overflow-auto rounded border border-solid border-gray-100 p-1 dark:border-gray-700">
        <div
          v-if="loading"
          class="flex h-full items-center justify-center">
          <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
        </div>
        <p
          v-else-if="error"
          class="m-0 break-all px-2 py-1 text-xs text-red-500 dark:text-red-400"
          data-test="dir-picker-error">
          {{ error }}
        </p>
        <template v-else>
          <button
            v-for="entry in level?.entries ?? []"
            :key="entry.path"
            type="button"
            class="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
            data-test="dir-picker-entry"
            :data-name="entry.name"
            @click="enter(entry)">
            <i class="pi pi-folder text-theme-main"></i>
            <span class="truncate">{{ entry.name }}</span>
          </button>
          <p
            v-if="!(level?.entries ?? []).length"
            class="m-0 px-2 py-1 text-xs text-gray-400 dark:text-gray-500"
            data-test="dir-picker-empty">
            {{ t('toolbar.projectDirectory.pickerEmpty') }}
          </p>
        </template>
      </div>

      <p
        v-if="level?.truncated"
        class="m-0 text-xs text-gray-400 dark:text-gray-500">
        {{
          t('toolbar.projectDirectory.pickerTruncated', {
            shown: level?.entries.length ?? 0,
            total: level?.total ?? 0
          })
        }}
      </p>

      <p
        v-if="state.error"
        class="m-0 break-all text-xs text-red-500 dark:text-red-400"
        data-test="dir-picker-bind-error">
        {{ state.error }}
      </p>

      <div class="flex justify-end gap-2">
        <Button
          :label="t('toolbar.projectDirectory.pickerCancel')"
          text
          severity="secondary"
          data-test="dir-picker-cancel"
          @click="visible = false" />
        <Button
          :label="t('toolbar.projectDirectory.pickerSelect')"
          icon="pi pi-check"
          :disabled="!currentPath || loading"
          data-test="dir-picker-select"
          @click="apply" />
      </div>
    </div>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SystemDirEntry, SystemDirLevel } from '~/composables/bridge';

const props = defineProps<{
  sessionId: string;
  /** Where the dialog opens; '' starts at the server user's home. */
  initialPath?: string;
}>();
const visible = defineModel<boolean>('visible', { required: true });

const { t } = useI18n();
const store = useProjectDirectoryStore();

const level = ref<SystemDirLevel | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const currentPath = computed(() => level.value?.path ?? '');
const state = computed(() => store.stateFor(props.sessionId));

/**
 * Load one level. A failed read keeps the dialog usable (up/retry still work)
 * and never binds anything.
 * @param path
 */
const load = async (path: string): Promise<void> => {
  loading.value = true;
  error.value = null;
  try {
    level.value = await fetchSystemDirs(path);
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('toolbar.projectDirectory.pickerLoadFailed');
  } finally {
    loading.value = false;
  }
};

const enter = (entry: SystemDirEntry): void => {
  void load(entry.path);
};

const goUp = (): void => {
  const parent = level.value?.parent;
  if (parent) void load(parent);
};

/** Apply the folder the dialog is standing in (the server validates it). */
const apply = async (): Promise<void> => {
  if (!currentPath.value) return;
  store.clearError(props.sessionId);
  await store.select(props.sessionId, currentPath.value);
  if (!store.stateFor(props.sessionId).error) visible.value = false;
};

// Reload on every open (and immediately when mounted open): the filesystem may
// have changed since the last look, and the initial path follows the (possibly
// parked) session binding.
watch(
  visible,
  isOpen => {
    if (!isOpen) return;
    store.clearError(props.sessionId);
    void load(props.initialPath ?? '');
  },
  { immediate: true }
);
</script>

<i18n lang="json">
{
  "en": {
    "toolbar": {
      "projectDirectory": {
        "pickerCancel": "Cancel",
        "pickerEmpty": "No subfolders here",
        "pickerHome": "Home",
        "pickerLoadFailed": "This folder could not be read",
        "pickerSelect": "Use this folder",
        "pickerTitle": "Choose a project directory",
        "pickerTruncated": "Showing the first {shown} of {total}",
        "pickerUp": "Up one level"
      }
    }
  },
  "zh": {
    "toolbar": {
      "projectDirectory": {
        "pickerCancel": "取消",
        "pickerEmpty": "此文件夹内没有子文件夹",
        "pickerHome": "主目录",
        "pickerLoadFailed": "无法读取该文件夹",
        "pickerSelect": "选择此文件夹",
        "pickerTitle": "选择项目目录",
        "pickerTruncated": "仅显示前 {shown} 个（共 {total} 个）",
        "pickerUp": "上一级"
      }
    }
  },
  "ja": {
    "toolbar": {
      "projectDirectory": {
        "pickerCancel": "キャンセル",
        "pickerEmpty": "このフォルダーにサブフォルダーはありません",
        "pickerHome": "ホーム",
        "pickerLoadFailed": "このフォルダーを読み取れませんでした",
        "pickerSelect": "このフォルダーを選択",
        "pickerTitle": "プロジェクトディレクトリを選択",
        "pickerTruncated": "先頭 {shown} 件のみ表示（全 {total} 件）",
        "pickerUp": "上の階層へ"
      }
    }
  },
  "ko": {
    "toolbar": {
      "projectDirectory": {
        "pickerCancel": "취소",
        "pickerEmpty": "이 폴더에는 하위 폴더가 없습니다",
        "pickerHome": "홈",
        "pickerLoadFailed": "이 폴더를 읽을 수 없습니다",
        "pickerSelect": "이 폴더 선택",
        "pickerTitle": "프로젝트 디렉터리 선택",
        "pickerTruncated": "처음 {shown}개만 표시 (전체 {total}개)",
        "pickerUp": "상위 폴더"
      }
    }
  }
}
</i18n>
