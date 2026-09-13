<template>
  <Dialog
    v-model:visible="visible"
    :header="t('config.persona.title')"
    :modal="true"
    :closable="true"
    class="persona-dialog w-[95vw] md:w-[1280px]"
    @show="onDialogShow">
    <div class="flex min-h-0 flex-1 flex-col gap-3 md:flex-row">
      <!-- Left column: existing 3-tab persona editor + save-preset action -->
      <div class="flex min-w-0 min-h-0 flex-1 flex-col gap-3">
        <div
          v-if="loading"
          class="flex items-center justify-center py-8">
          <ProgressSpinner style="width: 2rem; height: 2rem" />
        </div>
        <template v-else>
          <TabView
            v-model:activeIndex="activeTab"
            class="flex min-h-0 flex-1 flex-col">
            <TabPanel
              v-for="tab in tabs"
              :key="tab.key"
              :value="tab.key"
              :header="t(tab.i18nKey)">
              <div class="flex min-h-0 flex-1 flex-col gap-2">
                <div class="flex items-center justify-between">
                  <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
                  <div class="flex items-center gap-2">
                    <Button
                      :label="t('config.persona.restoreDefault')"
                      icon="pi pi-refresh"
                      severity="secondary"
                      text
                      size="small"
                      :loading="restoring"
                      :disabled="restoring"
                      @click="restoreDefault(tab)" />
                    <span
                      :class="[
                        'text-xs',
                        (editContent[tab.key]?.length ?? 0) > MAX_CHARS
                          ? 'text-red-500'
                          : (editContent[tab.key]?.length ?? 0) > MAX_CHARS * 0.9
                            ? 'text-orange-500'
                            : 'text-gray-400'
                      ]">
                      {{ editContent[tab.key]?.length ?? 0 }} / {{ MAX_CHARS }}
                    </span>
                  </div>
                </div>
                <Textarea
                  v-model="editContent[tab.key]"
                  rows="18"
                  class="w-full min-h-0 flex-1 font-mono text-sm"
                  :maxlength="MAX_CHARS"
                  style="overflow: auto" />
              </div>
            </TabPanel>
          </TabView>
          <div class="flex justify-end">
            <Button
              :label="t('config.persona.preset.savePreset')"
              icon="pi pi-save"
              severity="secondary"
              :loading="saving"
              :disabled="!actionEnabled"
              @click="savePreset" />
          </div>
        </template>
      </div>

      <!-- Right column: persona preset list -->
      <div class="flex w-full min-h-0 flex-col gap-2 md:w-[300px] md:shrink-0">
        <span class="text-sm font-semibold">{{ t('config.persona.preset.title') }}</span>
        <div class="flex max-h-[60vh] min-h-0 flex-1 flex-col gap-1 overflow-y-auto md:max-h-none">
          <!-- Virtual read-only entry: the default persona (Sherry) -->
          <div
            role="button"
            tabindex="0"
            class="flex cursor-pointer items-center justify-between gap-2 rounded-lg border border-solid border-gray-light bg-white px-3 py-2 text-sm text-theme-main transition-colors dark:border-[#555] dark:bg-[#2a2a36]/[0.6]"
            :class="{
              'bg-[#c1d6e5]!': activeDefault,
              'md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]': !activeDefault
            }"
            @click="selectDefault">
            <span class="truncate">{{ t('config.persona.preset.defaultName') }}</span>
            <span
              class="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {{ t('config.persona.preset.defaultBadge') }}
            </span>
          </div>
          <div
            v-if="presets.length === 0"
            class="px-3 py-1 text-xs text-gray-400">
            {{ t('config.persona.preset.emptyList') }}
          </div>
          <div
            v-for="preset in presets"
            :key="preset.id"
            role="button"
            tabindex="0"
            class="flex cursor-pointer items-center justify-between gap-2 rounded-lg border border-solid border-gray-light bg-white px-3 py-2 text-sm text-theme-main transition-colors dark:border-[#555] dark:bg-[#2a2a36]/[0.6]"
            :class="{
              'bg-[#c1d6e5]!': editingPresetId === preset.id,
              'md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]': editingPresetId !== preset.id
            }"
            @click="selectPreset(preset)">
            <span class="truncate">{{ preset.name }}</span>
            <span
              v-if="editingPresetId === preset.id"
              class="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {{ t('config.persona.preset.editingBadge') }}
            </span>
            <Button
              class="shrink-0"
              icon="pi pi-trash"
              severity="danger"
              text
              rounded
              size="small"
              :aria-label="t('common.delete')"
              @click.stop="requestDeletePreset(preset)" />
          </div>
        </div>
        <Button
          :label="t('config.persona.preset.apply')"
          icon="pi pi-check"
          class="w-full"
          :loading="applying"
          :disabled="!actionEnabled"
          @click="handleApply" />
      </div>
    </div>

    <!-- Name dialog: save current content as a new preset -->
    <Dialog
      v-model:visible="showNameDialog"
      :header="t('config.persona.preset.nameDialog.title')"
      :modal="true"
      :closable="true"
      :style="{ width: '360px' }">
      <div class="flex flex-col gap-2">
        <InputText
          v-model="presetName"
          :placeholder="t('config.persona.preset.nameDialog.placeholder')"
          :maxlength="50"
          @keydown.enter="confirmSavePreset" />
        <small
          v-if="nameError === 'duplicate'"
          class="text-red-500">
          {{ t('config.persona.preset.nameError.duplicate') }}
        </small>
        <small
          v-else-if="nameError === 'required'"
          class="text-red-500">
          {{ t('config.persona.preset.nameError.required') }}
        </small>
      </div>
      <template #footer>
        <div class="flex justify-end gap-2">
          <Button
            :label="t('config.cancel')"
            severity="secondary"
            @click="showNameDialog = false" />
          <Button
            :label="t('config.persona.preset.nameDialog.confirm')"
            icon="pi pi-save"
            :loading="saving"
            :disabled="!presetName.trim()"
            @click="confirmSavePreset" />
        </div>
      </template>
    </Dialog>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { PersonaPreset } from '@/composables/db';
import { logUtil } from '~/utils/log';

const { t, locale } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const MAX_CHARS = 2000;

interface PersonaTab {
  /** Unique key for the tab; the same basename may exist across layers. */
  key: string;
  /** Actual filename sent to the backend (e.g. 'SOUL.md'). */
  file: string;
  i18nKey: string;
  i18nDescKey: string;
  /** Read function that returns a file->content map for this tab's group. */
  readFn: () => Promise<Record<string, string>>;
}

const tabs: PersonaTab[] = [
  {
    key: 'SOUL.md',
    file: 'SOUL.md',
    i18nKey: 'config.tabs.soul',
    i18nDescKey: 'config.desc.soul',
    readFn: readSystemPrompt
  },
  {
    key: 'USER.md',
    file: 'USER.md',
    i18nKey: 'config.tabs.user',
    i18nDescKey: 'config.desc.user',
    readFn: readSystemPrompt
  }
] as const;

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
const restoring = ref(false);
const applying = ref(false);
const editContent = ref<Record<string, string>>({});
const originalContent = ref<Record<string, string>>({});

// Preset state machine: editingPresetId = the user preset currently loaded into the
// editor (null = default/new mode); activeDefault = the virtual "Sherry" entry highlight.
const editingPresetId = ref<number | null>(null);
const activeDefault = ref(false);

// Name dialog (save current content as a new preset) state.
const showNameDialog = ref(false);
const presetName = ref('');
const nameError = ref<'' | 'duplicate' | 'required'>('');

// Shared preset list singleton (auto-refreshes on first use).
const { presets, create, update, remove } = usePersonaPresets();

const onDialogShow = () => {
  void loadContent();
  editingPresetId.value = null;
  activeDefault.value = true;
  showNameDialog.value = false;
  presetName.value = '';
  nameError.value = '';
};

// If the main dialog is closed while the name dialog is open, close the latter too.
watch(visible, v => {
  if (!v) showNameDialog.value = false;
});

const loadContent = async () => {
  loading.value = true;
  try {
    // Group tabs by their read function so each group is fetched once.
    const byReadFn = new Map<PersonaTab['readFn'], PersonaTab[]>();
    for (const tab of tabs) {
      const list = byReadFn.get(tab.readFn) ?? [];
      list.push(tab);
      byReadFn.set(tab.readFn, list);
    }
    const content: Record<string, string> = {};
    for (const [readFn, groupTabs] of byReadFn) {
      const data = await readFn();
      for (const tab of groupTabs) {
        content[tab.key] = data[tab.file] ?? '';
      }
    }
    editContent.value = { ...content };
    originalContent.value = { ...content };
  } catch (e) {
    logUtil.e('[PersonaDialog] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

/** All tabs non-empty (trimmed) and each within the char limit — gates save & apply. */
const allTabsValid = computed(() =>
  tabs.every(tab => {
    const v = editContent.value[tab.file] ?? '';
    return v.trim().length > 0 && v.length <= MAX_CHARS;
  })
);

/** Enablement for both the 保存预设 ("Save Preset") and 应用 ("Apply") buttons. */
const actionEnabled = computed(
  () => allTabsValid.value && !loading.value && !saving.value && !applying.value && !restoring.value
);

/** Current editor content as a file->content map (all persona files). */
const buildContent = (): Record<string, string> => {
  const fileToContent: Record<string, string> = {};
  for (const tab of tabs) {
    fileToContent[tab.file] = editContent.value[tab.key] ?? '';
  }
  return fileToContent;
};

/**
 * Fill all tabs with the given file->content map.
 * @param content
 */
const fillTabs = (content: Record<string, string>) => {
  for (const tab of tabs) {
    editContent.value[tab.key] = content[tab.file] ?? '';
  }
};

const restoreDefault = async (tab: PersonaTab) => {
  restoring.value = true;
  try {
    const content = await readSystemPromptTemplate(locale.value);
    editContent.value[tab.key] = content[tab.file] ?? '';
  } catch (e) {
    logUtil.e('[PersonaDialog] Failed to restore default:', e);
  } finally {
    restoring.value = false;
  }
};

/** Click the virtual default entry: load the persona template into all tabs. */
const selectDefault = async () => {
  if (restoring.value || loading.value) return;
  restoring.value = true;
  try {
    const content = await readSystemPromptTemplate(locale.value);
    fillTabs(content);
    editingPresetId.value = null;
    activeDefault.value = true;
  } catch (e) {
    logUtil.e('[PersonaDialog] Failed to load persona template:', e);
  } finally {
    restoring.value = false;
  }
};

/**
 * Click a user preset entry: load its content into all tabs and enter edit mode.
 * @param preset
 */
const selectPreset = (preset: PersonaPreset) => {
  if (loading.value || restoring.value || preset.id === undefined) return;
  fillTabs(preset.content);
  editingPresetId.value = preset.id;
  activeDefault.value = false;
};

/**
 * Click 保存预设 ("Save Preset"):
 * - editing an existing preset → overwrite it directly (no name dialog);
 * - otherwise (default/new mode) → open the name dialog to save as a new preset.
 */
const savePreset = () => {
  if (!actionEnabled.value) return;
  if (editingPresetId.value !== null) {
    void overwriteEditingPreset();
  } else {
    presetName.value = '';
    nameError.value = '';
    showNameDialog.value = true;
  }
};

/** Direct-overwrite path for 保存预设 ("Save Preset") while editing a user preset. */
const overwriteEditingPreset = async () => {
  const id = editingPresetId.value;
  if (id === null) return;
  saving.value = true;
  try {
    const ok = await update(id, buildContent());
    if (ok) {
      toastSuccess(t('config.persona.preset.toast.presetSaved'));
    } else {
      toastError(t('config.persona.preset.toast.presetSaveFailed'));
    }
  } finally {
    saving.value = false;
  }
};

/** Confirm the name dialog: create a new preset (duplicate → inline error, dialog stays open). */
const confirmSavePreset = async () => {
  if (saving.value || !showNameDialog.value) return;
  const name = presetName.value.trim();
  if (!name) {
    nameError.value = 'required';
    return;
  }
  saving.value = true;
  try {
    const result = await create(name, buildContent());
    if (result.ok) {
      showNameDialog.value = false;
      // The just-created preset becomes the edited one (its entry is highlighted).
      editingPresetId.value = result.id;
      activeDefault.value = false;
      toastSuccess(t('config.persona.preset.toast.presetSaved'));
    } else if (result.reason === 'duplicate') {
      nameError.value = 'duplicate';
    } else {
      toastError(t('config.persona.preset.toast.presetSaveFailed'));
    }
  } finally {
    saving.value = false;
  }
};

// PrimeVue ConfirmationService (ConfirmDialog mounted in app.vue) — same pattern as SessionSidebar.
const confirm = useConfirm();

/**
 * Delete a preset after a second confirmation.
 * @param preset
 */
const requestDeletePreset = (preset: PersonaPreset) => {
  if (preset.id === undefined) return;
  confirm.require({
    header: t('config.persona.preset.confirmDelete.title'),
    message: t('config.persona.preset.confirmDelete.message', { name: preset.name }),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('config.cancel'), severity: 'secondary' },
    accept: () => {
      void doRemovePreset(preset);
    }
  });
};

/**
 * Actual delete executor (triggered by the confirmation dialog accept callback).
 * @param preset
 */
const doRemovePreset = async (preset: PersonaPreset) => {
  const id = preset.id;
  if (id === undefined) return;
  const ok = await remove(id);
  if (!ok) return;
  toastSuccess(t('config.persona.preset.toast.presetDeleted'));
  if (editingPresetId.value === id) {
    // The deleted preset was being edited: reset the edit state (no entry highlighted).
    editingPresetId.value = null;
    activeDefault.value = false;
  }
};

/** Click 应用 ("Apply"): full-write all persona files; success → toast + saved + close, failure → stay open. */
const handleApply = async () => {
  if (!actionEnabled.value) return;
  applying.value = true;
  try {
    const snapshot = buildContent();
    await writeSystemPrompt(snapshot);
    // Verify the write actually landed: in browser mode fetchApi swallows request
    // failures (retries 3x, then resolves null instead of throwing), so a failed
    // PUT would otherwise be indistinguishable from success here. Read the files
    // back and compare; any read failure or mismatch → treat the apply as failed.
    const written = await readSystemPrompt();
    const verified = !!written && tabs.every(tab => written[tab.file] === snapshot[tab.file]);
    if (!verified) {
      throw new Error('[PersonaDialog] applied content verification failed');
    }
    emits('saved');
    visible.value = false;
    toastSuccess(t('config.persona.preset.toast.applySuccess'));
  } catch (e) {
    logUtil.e('[PersonaDialog] Failed to apply persona:', e);
    toastError(t('config.persona.preset.toast.applyFailed'));
  } finally {
    applying.value = false;
  }
};
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "persona": {
        "title": "AI人格",
        "restoreDefault": "恢复默认",
        "preset": {
          "title": "预设人格",
          "savePreset": "保存预设",
          "apply": "应用",
          "editingBadge": "编辑中",
          "defaultBadge": "默认",
          "emptyList": "暂无预设",
          "defaultName": "橘雪莉",
          "nameDialog": {
            "title": "保存为预设",
            "placeholder": "输入预设名称",
            "confirm": "保存"
          },
          "nameError": {
            "duplicate": "名称已存在",
            "required": "名称不能为空"
          },
          "confirmDelete": {
            "title": "删除预设",
            "message": "确定删除预设「{name}」吗？此操作不可恢复"
          },
          "toast": {
            "presetSaved": "预设已保存",
            "presetDeleted": "预设已删除",
            "applyFailed": "应用失败，请检查后端连接",
            "presetSaveFailed": "预设保存失败",
            "applySuccess": "应用成功"
          }
        }
      },
      "tabs": {
        "soul": "人格灵魂",
        "user": "用户信息"
      },
      "desc": {
        "soul": "Agent人格、语气、性格",
        "user": "用户信息和偏好"
      }
    }
  },
  "en": {
    "config": {
      "persona": {
        "title": "AI Persona",
        "restoreDefault": "Restore Default",
        "preset": {
          "title": "Preset Personas",
          "savePreset": "Save Preset",
          "apply": "Apply",
          "editingBadge": "Editing",
          "defaultBadge": "Default",
          "emptyList": "No presets yet",
          "defaultName": "Tachibana Sherry",
          "nameDialog": {
            "title": "Save as Preset",
            "placeholder": "Enter preset name",
            "confirm": "Save"
          },
          "nameError": {
            "duplicate": "Name already exists",
            "required": "Name is required"
          },
          "confirmDelete": {
            "title": "Delete Preset",
            "message": "Delete preset \"{name}\"? This cannot be undone"
          },
          "toast": {
            "presetSaved": "Preset saved",
            "presetDeleted": "Preset deleted",
            "applyFailed": "Apply failed, check backend connection",
            "presetSaveFailed": "Failed to save preset",
            "applySuccess": "Applied successfully"
          }
        }
      },
      "tabs": {
        "soul": "Soul",
        "user": "User Profile"
      },
      "desc": {
        "soul": "Agent personality, tone, character",
        "user": "User info and preferences"
      }
    }
  },
  "ja": {
    "config": {
      "persona": {
        "title": "AI人格",
        "restoreDefault": "デフォルトに戻す",
        "preset": {
          "title": "プリセット人格",
          "savePreset": "プリセット保存",
          "apply": "適用",
          "editingBadge": "編集中",
          "defaultBadge": "デフォルト",
          "emptyList": "プリセットなし",
          "defaultName": "橘雪莉",
          "nameDialog": {
            "title": "プリセットとして保存",
            "placeholder": "プリセット名を入力",
            "confirm": "保存"
          },
          "nameError": {
            "duplicate": "名前は既に存在します",
            "required": "名前は必須です"
          },
          "confirmDelete": {
            "title": "プリセットを削除",
            "message": "プリセット「{name}」を削除しますか？元に戻せません"
          },
          "toast": {
            "presetSaved": "プリセットを保存しました",
            "presetDeleted": "プリセットを削除しました",
            "applyFailed": "適用に失敗しました。バックエンド接続を確認してください",
            "presetSaveFailed": "プリセットの保存に失敗しました",
            "applySuccess": "適用しました"
          }
        }
      },
      "tabs": {
        "soul": "人格・魂",
        "user": "ユーザー情報"
      },
      "desc": {
        "soul": "Agent 人格、トーン、性格",
        "user": "ユーザー情報と好み"
      }
    }
  },
  "ko": {
    "config": {
      "persona": {
        "title": "AI 페르소나",
        "restoreDefault": "기본값 복원",
        "preset": {
          "title": "프리셋 페르소나",
          "savePreset": "프리셋 저장",
          "apply": "적용",
          "editingBadge": "편집 중",
          "defaultBadge": "기본",
          "emptyList": "프리셋 없음",
          "defaultName": "橘雪莉",
          "nameDialog": {
            "title": "프리셋으로 저장",
            "placeholder": "프리셋 이름 입력",
            "confirm": "저장"
          },
          "nameError": {
            "duplicate": "이름이 이미 존재합니다",
            "required": "이름은 필수입니다"
          },
          "confirmDelete": {
            "title": "프리셋 삭제",
            "message": "프리셋 \"{name}\"을(를) 삭제하시겠습니까? 되돌릴 수 없습니다"
          },
          "toast": {
            "presetSaved": "프리셋이 저장되었습니다",
            "presetDeleted": "프리셋이 삭제되었습니다",
            "applyFailed": "적용 실패, 백엔드 연결을 확인하세요",
            "presetSaveFailed": "프리셋 저장 실패",
            "applySuccess": "적용됨"
          }
        }
      },
      "tabs": {
        "soul": "인격·영혼",
        "user": "사용자 정보"
      },
      "desc": {
        "soul": "Agent 인격, 어조, 성격",
        "user": "사용자 정보 및 선호도"
      }
    }
  }
}
</i18n>

<style scoped>
/* 让对话框内容区成为 flex 列容器：根布局用 flex-1 精确填充内容区高度，
   内部 flex 链（TabView → panels → panel → textarea）自适应伸缩，
   避免 72vh 等固定高度把内容区撑出垂直滚动条。
   Dialog 被 Teleport 到 <body>，scoped 的选择器（依赖 data-v 祖先）匹配不到
   .p-dialog-content，所以用非 scoped 规则 + 唯一标记类 .persona-dialog 精准锁定。 */
:deep(.p-tabview-panels) {
  display: flex;
  flex-direction: column;
  flex: 1 1 0%;
  min-height: 0;
}

:deep(.p-tabview-panel) {
  display: flex;
  flex: 1 1 0%;
  min-height: 0;
}
</style>

<style>
.persona-dialog > .p-dialog-content {
  display: flex;
  flex-direction: column;
}

/* 让对话框填满可用高度（PrimeVue 默认 max-height:90% 但高度内容驱动，
   不撑满的话 textarea 固有高度(rows)决定大小）。窗口高度 >=600px 时撑满 90vh，
   低于 600px 保持内容驱动，避免极端小窗下各区域被过度压缩。 */
@media (min-height: 600px) {
  .persona-dialog {
    height: 90vh;
  }
}
</style>
