<template>
  <ToolbarPopover>
    <template #trigger="{ toggle }">
      <!-- Collapsed chip: the folder glyph plus the bound directory's basename.
           Unbound sessions carry a warning tone instead of a path, so "the agent
           is working in the sherry checkout" is visible at a glance rather than
           buried in a setting. A parked choice shows the clock icon (same visual
           language as the model/thinking controls). -->
      <div class="flex items-center gap-1">
        <button
          type="button"
          data-test="project-dir-trigger"
          class="flex items-center gap-1 rounded px-1.5 py-0.5 text-xs transition-colors"
          :class="
            bound
              ? 'text-theme-main hover:bg-gray-100 dark:hover:bg-gray-800'
              : 'text-amber-500 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-900/20'
          "
          :title="triggerTitle"
          :aria-label="triggerTitle"
          @click="onOpen(toggle)">
          <i :class="bound ? 'pi pi-folder' : 'pi pi-exclamation-triangle'"></i>
          <span class="max-w-[10rem] truncate">{{ label }}</span>
        </button>
        <i
          v-if="state.pendingDirectory"
          data-test="project-dir-pending"
          class="pi pi-clock text-xs text-theme-main"
          :title="t('toolbar.projectDirectory.pendingHint')"
          :aria-label="t('toolbar.projectDirectory.pendingHint')"></i>
      </div>
    </template>

    <div
      class="flex w-80 flex-col gap-2 p-1"
      data-test="project-dir-panel">
      <p class="m-0 text-xs font-medium text-gray-500 dark:text-gray-400">
        {{ t('toolbar.projectDirectory.label') }}
      </p>
      <p
        class="m-0 break-all text-xs text-gray-600 dark:text-gray-300"
        data-test="project-dir-effective">
        {{ state.effective || t('toolbar.projectDirectory.unbound') }}
        <span
          v-if="!bound"
          class="text-amber-500 dark:text-amber-400"
          >（{{ t('toolbar.projectDirectory.unbound') }}）</span
        >
      </p>
      <p
        v-if="state.pendingDirectory"
        class="m-0 break-all text-xs text-theme-main">
        {{ t('toolbar.projectDirectory.pendingLabel') }}：{{ state.pendingDirectory }}
      </p>

      <div class="flex flex-col gap-1">
        <!-- One action, two runtimes: the desktop build opens the OS folder
             dialog, the browser build opens the in-app folder picker (a browser
             cannot hand out an absolute path, so it browses the server's
             filesystem through `/system/dirs` instead of typing one). -->
        <Button
          :label="t('toolbar.projectDirectory.browse')"
          icon="pi pi-folder-open"
          size="small"
          data-test="project-dir-browse"
          @click="choose" />
      </div>

      <p
        v-if="state.error"
        class="m-0 break-all text-xs text-red-500 dark:text-red-400"
        data-test="project-dir-error">
        {{ state.error }}
      </p>
    </div>
  </ToolbarPopover>

  <!-- Sibling of the popover, not a child of its panel: the panel is v-if-gated
       and would take the dialog down with it on any outside pointerdown. -->
  <ProjectDirectoryPicker
    v-model:visible="pickerOpen"
    :session-id="sessionId"
    :initial-path="state.directory ?? ''" />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
// Siblings under pages/home/components are NOT auto-imported (only app/components
// is), so every component usage needs an explicit import.
import ProjectDirectoryPicker from './ProjectDirectoryPicker.vue';
import ToolbarPopover from './ToolbarPopover.vue';

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
/** Per-session project-directory control store (hydrated from the backend). */
const store = useProjectDirectoryStore();

/** Whether the in-app folder picker (browser build) is open. */
const pickerOpen = ref(false);

const state = computed(() => store.stateFor(props.sessionId));
const bound = computed(() => !!state.value.directory);
const label = computed(() => {
  const dir = state.value.directory;
  if (!dir) return t('toolbar.projectDirectory.unbound');
  const parts = dir.replace(/\/+$/, '').split('/');
  return parts[parts.length - 1] || dir;
});
const triggerTitle = computed(() => {
  const dir = state.value.directory;
  return dir ? dir : `${t('toolbar.projectDirectory.unbound')}：${state.value.effective}`;
});

/**
 * Whether the Tauri-native folder picker is usable in this build. Where it is
 * not, the in-app picker dialog takes over, so the action always exists.
 */
const canBrowse = computed(() => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window);

/**
 * Re-hydrate whenever the panel opens (the server is the only authority on what
 * is currently bound).
 * @param toggle
 */
const onOpen = (toggle: () => void): void => {
  store.clearError(props.sessionId);
  void store.hydrate(props.sessionId);
  toggle();
};

/** Open the system folder dialog (desktop) or the in-app folder picker. */
const choose = async (): Promise<void> => {
  if (!canBrowse.value) {
    pickerOpen.value = true;
    return;
  }
  try {
    const picked = await pickProjectDirectory();
    if (!picked) return;
    await store.select(props.sessionId, picked);
  } catch (e) {
    store.fail(props.sessionId, e instanceof Error ? e.message : String(e));
  }
};

/** Keep the chip honest when the session id changes (KeepAlive instances). */
watch(
  () => props.sessionId,
  sid => {
    if (sid) void store.hydrate(sid);
  },
  { immediate: true }
);
</script>

<i18n lang="json">
{
  "en": {
    "toolbar": {
      "projectDirectory": {
        "browse": "Choose folder…",
        "label": "Project directory",
        "pendingHint": "Applies on the next turn",
        "pendingLabel": "Pending",
        "unbound": "No project directory bound"
      }
    }
  },
  "zh": {
    "toolbar": {
      "projectDirectory": {
        "browse": "选择文件夹…",
        "label": "项目目录",
        "pendingHint": "将于下一轮生效",
        "pendingLabel": "待生效",
        "unbound": "未绑定项目目录"
      }
    }
  },
  "ja": {
    "toolbar": {
      "projectDirectory": {
        "browse": "フォルダーを選択…",
        "label": "プロジェクトディレクトリ",
        "pendingHint": "次のターンで有効になります",
        "pendingLabel": "保留中",
        "unbound": "プロジェクトディレクトリ未設定"
      }
    }
  },
  "ko": {
    "toolbar": {
      "projectDirectory": {
        "browse": "폴더 선택…",
        "label": "프로젝트 디렉터리",
        "pendingHint": "다음 턴에 적용됩니다",
        "pendingLabel": "대기 중",
        "unbound": "프로젝트 디렉터리 미설정"
      }
    }
  }
}
</i18n>
