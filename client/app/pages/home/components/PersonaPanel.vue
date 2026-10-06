<template>
  <!-- Right-sidebar tab body: mounted/unmounted with the tab, which drives the load
       (the sidebar owns the tab label and the close button). The two columns below
       stay md:flex-row and stack when the panel is narrow. -->
  <div class="flex flex-col h-full min-h-0 overflow-y-auto p-4">
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
            <!-- FIRST tab: the role config (who the AI plays, who the user plays).
                 Part of the preset: 保存预设 stores the names + avatars with the
                 persona files, and 应用 writes the composed ROLE.md so the role
                 statement reaches the system prompt. -->
            <TabPanel
              value="role"
              :header="t('config.tabs.role')">
              <div
                class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
                data-test="persona-role-tab">
                <div class="flex items-center justify-between">
                  <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('config.desc.role') }}</span>
                  <Button
                    :label="t('config.persona.restoreDefault')"
                    icon="pi pi-refresh"
                    severity="secondary"
                    text
                    size="small"
                    @click="restoreRoleDefault" />
                </div>
                <p class="m-0 text-xs font-medium text-red-600 dark:text-red-400">{{ t('config.role.charNote') }}</p>

                <!-- AI role -->
                <div class="flex flex-col gap-2">
                  <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{
                    t('config.role.assistant')
                  }}</span>
                  <div class="flex items-center gap-3">
                    <!-- An empty avatar field means "no avatar of its own": the neutral
                         gray silhouette renders in its place (never a broken image). -->
                    <img
                      :src="charAssistant.avatar || DEFAULT_PLACEHOLDER_AVATAR"
                      alt="assistant avatar"
                      class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                    <div class="flex flex-col gap-2 flex-1">
                      <InputText
                        v-model="charAssistant.name"
                        data-test="persona-role-ai-name"
                        :placeholder="t('config.role.aiName')"
                        class="w-full" />
                      <div class="flex items-center gap-2">
                        <FileUpload
                          mode="basic"
                          :choose-label="t('config.uploadAvatar')"
                          accept="image/*"
                          customUpload
                          :auto="false"
                          @select="onAssistAvatarSelect">
                          <template #filelabel="{ files }">
                            <span class="text-xs text-gray-400">
                              {{ avatarFileLabel(Array.isArray(files) ? files : []) }}
                            </span>
                          </template>
                        </FileUpload>
                        <Button
                          :label="t('config.role.resetAvatar')"
                          icon="pi pi-replay"
                          severity="secondary"
                          text
                          size="small"
                          :disabled="!charAssistant.avatar"
                          data-test="persona-role-ai-avatar-reset"
                          @click="resetAvatar('assistant')" />
                      </div>
                    </div>
                  </div>
                </div>

                <Divider />

                <!-- User role -->
                <div class="flex flex-col gap-2">
                  <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{
                    t('config.role.userRole')
                  }}</span>
                  <div class="flex items-center gap-3">
                    <img
                      :src="charUser.avatar || DEFAULT_PLACEHOLDER_AVATAR"
                      alt="user avatar"
                      class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                    <div class="flex flex-col gap-2 flex-1">
                      <InputText
                        v-model="charUser.name"
                        data-test="persona-role-user-name"
                        :placeholder="t('config.role.userName')"
                        class="w-full" />
                      <div class="flex items-center gap-2">
                        <FileUpload
                          mode="basic"
                          :choose-label="t('config.uploadAvatar')"
                          accept="image/*"
                          customUpload
                          :auto="false"
                          @select="onUserAvatarSelect">
                          <template #filelabel="{ files }">
                            <span class="text-xs text-gray-400">
                              {{ avatarFileLabel(Array.isArray(files) ? files : []) }}
                            </span>
                          </template>
                        </FileUpload>
                        <Button
                          :label="t('config.role.resetAvatar')"
                          icon="pi pi-replay"
                          severity="secondary"
                          text
                          size="small"
                          :disabled="!charUser.avatar"
                          data-test="persona-role-user-avatar-reset"
                          @click="resetAvatar('user')" />
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </TabPanel>
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
          <!-- Virtual read-only entries: the built-in personas (no delete button,
               not Dexie rows). 橘雪莉 = the shipped default; 编程助手 = a plain
               assistant template (operating rules only, no soul / user profile). -->
          <div
            v-for="builtin in BUILTIN_PRESETS"
            :key="builtin.id"
            role="button"
            tabindex="0"
            class="flex cursor-pointer items-center justify-between gap-2 rounded-lg border border-solid border-gray-light bg-white px-3 py-2 text-sm text-theme-main transition-colors dark:border-[#555] dark:bg-[#2a2a36]/[0.6]"
            :class="{
              'bg-[#c1d6e5]!': activeBuiltin === builtin.id,
              'md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]': activeBuiltin !== builtin.id
            }"
            :data-test="`builtin-${builtin.id}`"
            @click="selectBuiltin(builtin.id)">
            <span class="truncate">{{ t(builtin.nameKey) }}</span>
            <span
              class="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {{ t(builtin.badgeKey) }}
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

    <!-- Avatar crop (role tab): 1:1, output 512×512 — moved here with the role config -->
    <AvatarCropDialog
      v-model="cropVisible"
      :src="cropSource"
      :aspect-ratio="1"
      :output-width="512"
      :output-height="512"
      :header="cropVisible ? t('config.crop.title') : ''"
      @cropped="onCropConfirmed" />
  </div>
</template>

<script lang="ts" setup>
import { ref, computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import AvatarCropDialog from './AvatarCropDialog.vue';
import type { PersonaPreset, PresetCharacter } from '@/composables/db';
// DEFAULT_CACHED_CHARACTER / DEFAULT_PLACEHOLDER_AVATAR are auto-imported from
// ~/composables/defaultCharacter.
import { logUtil } from '~/utils/log';

const { t, locale } = useI18n({ useScope: 'local' });

const emits = defineEmits<{ saved: [] }>();

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
    key: 'AGENTS.md',
    file: 'AGENTS.md',
    i18nKey: 'config.tabs.agents',
    i18nDescKey: 'config.desc.agents',
    readFn: readSystemPrompt
  },
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

/** The role statement file the role tab owns (composed, not a textarea tab). */
const ROLE_FILE = 'ROLE.md';

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
const restoring = ref(false);
const applying = ref(false);
const editContent = ref<Record<string, string>>({});
const originalContent = ref<Record<string, string>>({});

// ── Role config (the first tab): both role names + avatars ─────────────────
// The names are display info (Dexie global profile, locked per session on first
// open) AND prompt content: 应用 composes ROLE.md from them in the active UI
// language, so the agent reads who it plays and who the user plays.
const charAssistant = ref({ name: DEFAULT_CACHED_CHARACTER.aiName, avatar: DEFAULT_CACHED_CHARACTER.aiAvatar });
const charUser = ref({ name: DEFAULT_CACHED_CHARACTER.userName, avatar: DEFAULT_CACHED_CHARACTER.userAvatar });
/** Snapshot of the loaded character (Dexie writes only happen when something changed). */
const originalChar = ref<PresetCharacter>({
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar,
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar
});

// Avatar crop state (1:1; the crop dialog itself is shared with the background editor)
const cropVisible = ref(false);
const cropSource = ref('');
const cropTarget = ref<'user' | 'assistant'>('user');

// Preset state machine: editingPresetId = the user preset currently loaded into the
// editor (null = built-in / new mode); activeBuiltin = which virtual built-in entry
// is loaded. The DEFAULT preset (编程助手) starts highlighted; the list itself
// (BUILTIN_PRESETS + the saved presets) is defined in the shared catalogue, so the
// new-session dialog and the top-bar viewer render the identical order and badges.
const editingPresetId = ref<number | null>(null);
const activeBuiltin = ref<BuiltinPresetId | null>(DEFAULT_PRESET_ID);

// Name dialog (save current content as a new preset) state.
const showNameDialog = ref(false);
const presetName = ref('');
const nameError = ref<'' | 'duplicate' | 'required'>('');

// Shared preset list singleton (auto-refreshes on first use).
const { presets, create, update, remove } = usePersonaPresets();

const onDialogShow = () => {
  void loadContent();
  editingPresetId.value = null;
  activeBuiltin.value = 'sherry';
  showNameDialog.value = false;
  presetName.value = '';
};

// The panel's mount lifetime drives the load (replaces the dialog's @show).
onMounted(onDialogShow);

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

    // Role names/avatars come from the local Dexie global profile (the same row
    // the old 系统配置-角色配置 tab edited); ROLE.md itself is NOT parsed back —
    // the compose direction is one-way (names -> sentence).
    const charData = await readCachedCharacter(GLOBAL_SESSION_KEY);
    fillCharacter({
      aiName: charData?.aiName?.trim() ? charData.aiName : DEFAULT_CACHED_CHARACTER.aiName,
      aiAvatar: charData?.aiAvatar ?? DEFAULT_CACHED_CHARACTER.aiAvatar,
      userName: charData?.userName?.trim() ? charData.userName : DEFAULT_CACHED_CHARACTER.userName,
      userAvatar: charData?.userAvatar ?? DEFAULT_CACHED_CHARACTER.userAvatar
    });
    originalChar.value = buildPresetCharacter();
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

/**
 * Gate for 保存预设 / 应用: every file tab within the char limit. Empty content
 * is LEGAL — the 编程助手 built-in ships an empty soul / user profile (a plain
 * assistant without role-play) and an empty user role name means "the user
 * plays nothing" — so no non-empty requirement remains. The backend accepts
 * blank persona writes for the same reason.
 */
const allTabsValid = computed(() => tabs.every(tab => (editContent.value[tab.file] ?? '').length <= MAX_CHARS));

/** Enablement for both the 保存预设 ("Save Preset") and 应用 ("Apply") buttons. */
const actionEnabled = computed(
  () => allTabsValid.value && !loading.value && !saving.value && !applying.value && !restoring.value
);

/**
 * The role names + avatars as one snapshot (the preset's `character` block).
 * @returns The current role state.
 */
const buildPresetCharacter = (): PresetCharacter => ({
  aiName: charAssistant.value.name,
  aiAvatar: charAssistant.value.avatar,
  userName: charUser.value.name,
  userAvatar: charUser.value.avatar
});

/**
 * Current role state from a preset / the defaults / the loaded profile.
 * @param character Role names + avatars to load into the tab.
 */
const fillCharacter = (character: PresetCharacter) => {
  charAssistant.value = { name: character.aiName, avatar: character.aiAvatar };
  charUser.value = { name: character.userName, avatar: character.userAvatar };
};

/**
 * File map for the persona API: the edited files + the composed role file
 * (`composeRoleFile` in the shared catalogue states the roles, in the current UI
 * language, and omits a blank name entirely).
 * @returns The full apply payload written through `writeSystemPrompt`.
 */
const buildApplyContent = (): Record<string, string> => {
  const fileToContent: Record<string, string> = {};
  for (const tab of tabs) {
    fileToContent[tab.file] = editContent.value[tab.key] ?? '';
  }
  fileToContent[ROLE_FILE] = composeRoleFile(buildPresetCharacter(), t);
  return fileToContent;
};

/**
 * File map stored in a preset: only the files the user edits. ROLE.md is
 * recomposed from the preset's `character` on apply, so a stale sentence can
 * never be replayed (and never drifts from the names).
 * @returns The file map stored in a persona preset.
 */
const buildPresetContent = (): Record<string, string> => {
  const fileToContent: Record<string, string> = {};
  for (const tab of tabs) {
    fileToContent[tab.file] = editContent.value[tab.key] ?? '';
  }
  return fileToContent;
};

/**
 * Fill all file tabs with the given file->content map.
 * @param content File basename -> content (extra files, e.g. ROLE.md, are ignored here).
 * @returns Nothing.
 */
const fillTabs = (content: Record<string, string>) => {
  for (const tab of tabs) {
    editContent.value[tab.key] = content[tab.file] ?? '';
  }
};

/**
 * Localized label for the avatar FileUpload (`#filelabel` slot, replacing the
 * browser-native "No file chosen"): file selected → its name; otherwise the prompt.
 * @param files Selected files from the FileUpload event.
 * @returns The label text for the upload row.
 */
const avatarFileLabel = (files: File[]): string => {
  if (files.length > 0) return files[0]?.name ?? '';
  return t('config.role.noFileChosen');
};

/**
 * Reads an uploaded image file as a base64 data URL.
 * @param file Image file to read.
 * @returns The data URL, or '' when the reader produced no string.
 */
const readFileAsDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

const onUserAvatarSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  void openAvatarCrop('user', file);
};

const onAssistAvatarSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  void openAvatarCrop('assistant', file);
};

/**
 * Open the 1:1 crop dialog for an avatar pick.
 * @param target Which role the avatar belongs to.
 * @param file
 */
const openAvatarCrop = async (target: 'user' | 'assistant', file: File) => {
  try {
    cropTarget.value = target;
    cropSource.value = await readFileAsDataUrl(file);
    cropVisible.value = true;
  } catch (e) {
    logUtil.e('[PersonaPanel] Image read failed:', e);
  }
};

/**
 * Apply the crop result to the role the dialog was opened for.
 * @param dataUrl
 */
const onCropConfirmed = (dataUrl: string) => {
  if (cropTarget.value === 'user') {
    charUser.value.avatar = dataUrl;
  } else {
    charAssistant.value.avatar = dataUrl;
  }
  cropVisible.value = false;
};

/** Reset the role tab to the built-in default names + avatars (mirrors 恢复默认). */
const restoreRoleDefault = () => {
  fillCharacter({ ...DEFAULT_CACHED_CHARACTER });
};

/**
 * Clear ONE role's avatar back to the neutral placeholder ("reset to the
 * default avatar"): the field becomes empty — an empty avatar means "no avatar
 * of its own" and every render site falls back to the auto-imported
 * `DEFAULT_PLACEHOLDER_AVATAR` gray silhouette. The name is left untouched, so
 * this is the per-avatar sibling of 恢复默认.
 * @param target Which role the avatar belongs to.
 */
const resetAvatar = (target: 'user' | 'assistant') => {
  if (target === 'user') {
    charUser.value.avatar = '';
    return;
  }
  charAssistant.value.avatar = '';
};

const restoreDefault = async (tab: PersonaTab) => {
  restoring.value = true;
  try {
    const content = await readSystemPromptTemplate(locale.value);
    editContent.value[tab.key] = content[tab.file] ?? '';
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to restore default:', e);
  } finally {
    restoring.value = false;
  }
};

/**
 * Click a built-in entry: load its shipped template through the shared catalogue
 * (the same payload the new-session dialog and the top-bar viewer use).
 * - 编程助手 → the operating rules only: empty soul and user profile, and BOTH
 *   role names empty — a plain coding assistant, no role statement at all;
 * - 橘雪莉 → the full language template plus the default names/avatars.
 * @param id Which built-in entry was clicked.
 */
const selectBuiltin = async (id: BuiltinPresetId) => {
  if (restoring.value || loading.value) return;
  restoring.value = true;
  try {
    const template = await readSystemPromptTemplate(locale.value);
    const payload = builtinPayload(id, template, t);
    fillTabs(payload.content);
    fillCharacter(payload.character);
    editingPresetId.value = null;
    activeBuiltin.value = id;
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to load persona template:', e);
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
  // Presets saved before the role tab existed carry no character block: leave
  // the current roles untouched rather than silently resetting them.
  if (preset.character) fillCharacter(preset.character);
  editingPresetId.value = preset.id;
  activeBuiltin.value = null;
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
    const ok = await update(id, buildPresetContent(), buildPresetCharacter());
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
    const result = await create(name, buildPresetContent(), buildPresetCharacter());
    if (result.ok) {
      showNameDialog.value = false;
      // The just-created preset becomes the edited one (its entry is highlighted).
      editingPresetId.value = result.id;
      activeBuiltin.value = null;
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
    activeBuiltin.value = null;
  }
};

/**
 * Write the role names/avatars to the local Dexie global profile when they
 * changed — the "global pending profile" new sessions copy their display
 * snapshot from, exactly as the old 系统配置-角色配置 tab wrote it.
 */
const persistCharacter = async () => {
  const current = buildPresetCharacter();
  const changed =
    current.userName !== originalChar.value.userName ||
    current.userAvatar !== originalChar.value.userAvatar ||
    current.aiName !== originalChar.value.aiName ||
    current.aiAvatar !== originalChar.value.aiAvatar;
  if (!changed) return;
  await cacheCharacter({
    session_id: GLOBAL_SESSION_KEY,
    userName: current.userName,
    userAvatar: current.userAvatar,
    aiName: current.aiName,
    aiAvatar: current.aiAvatar
  });
  originalChar.value = current;
};

/** Click 应用 ("Apply"): full-write the persona files (incl. the composed ROLE.md) + the role profile; success → toast + saved + close, failure → stay open. */
const handleApply = async () => {
  if (!actionEnabled.value) return;
  applying.value = true;
  try {
    const snapshot = buildApplyContent();
    await writeSystemPrompt(snapshot);
    // Verify the write actually landed: in browser mode fetchApi swallows request
    // failures (retries 3x, then resolves null instead of throwing), so a failed
    // PUT would otherwise be indistinguishable from success here. Read the files
    // back and compare; any read failure or mismatch → treat the apply as failed.
    const written = await readSystemPrompt();
    const verified = !!written && Object.entries(snapshot).every(([file, content]) => written[file] === content);
    if (!verified) {
      throw new Error('[PersonaPanel] applied content verification failed');
    }
    await persistCharacter();
    emits('saved');
    toastSuccess(t('config.persona.preset.toast.applySuccess'));
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to apply persona:', e);
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
        "restoreDefault": "恢复默认",
        "preset": {
          "savePreset": "保存预设",
          "apply": "应用",
          "editingBadge": "编辑中",
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
      "desc": {
        "role": "AI 与用户各自的扮演角色（名字与头像）",
        "agents": "Agent 的运行守则与安全边界",
        "soul": "Agent人格、语气、性格",
        "user": "用户信息和偏好"
      },
      "uploadAvatar": "上传头像",
      "crop": {
        "title": "裁剪头像"
      },
      "role": {
        "charNote": "名字与头像修改仅在新建会话后生效，旧会话不受影响；角色名字同时写入系统提示词。",
        "noFileChosen": "可选择新的头像图片",
        "resetAvatar": "重置头像"
      }
    }
  },
  "en": {
    "config": {
      "persona": {
        "restoreDefault": "Restore Default",
        "preset": {
          "savePreset": "Save Preset",
          "apply": "Apply",
          "editingBadge": "Editing",
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
      "desc": {
        "role": "Who the AI and the user each play (names and avatars)",
        "agents": "The agent's operating instructions and safety boundaries",
        "soul": "Agent personality, tone, character",
        "user": "User info and preferences"
      },
      "uploadAvatar": "Upload Avatar",
      "crop": {
        "title": "Crop Avatar"
      },
      "role": {
        "charNote": "Name and avatar changes only take effect in new sessions; existing sessions are not affected. The role names are also written into the system prompt.",
        "noFileChosen": "Select an avatar image file",
        "resetAvatar": "Reset avatar"
      }
    }
  },
  "ja": {
    "config": {
      "persona": {
        "restoreDefault": "デフォルトに戻す",
        "preset": {
          "savePreset": "プリセット保存",
          "apply": "適用",
          "editingBadge": "編集中",
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
      "desc": {
        "role": "AI とユーザーがそれぞれ演じる役割（名前とアバター）",
        "agents": "Agent の操作指示と安全境界",
        "soul": "Agent 人格、トーン、性格",
        "user": "ユーザー情報と好み"
      },
      "uploadAvatar": "アバターをアップロード",
      "crop": {
        "title": "アバターをトリミング"
      },
      "role": {
        "charNote": "名前とアバターの変更は新しいセッション作成後にのみ反映され、既存のセッションには影響しません。役割名はシステムプロンプトにも書き込まれます。",
        "noFileChosen": "新しいアバター画像を選択できます",
        "resetAvatar": "アバターをリセット"
      }
    }
  },
  "ko": {
    "config": {
      "persona": {
        "restoreDefault": "기본값 복원",
        "preset": {
          "savePreset": "프리셋 저장",
          "apply": "적용",
          "editingBadge": "편집 중",
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
      "desc": {
        "role": "AI와 사용자가 각각 맡는 역할(이름과 아바타)",
        "agents": "Agent의 운영 지침과 안전 경계",
        "soul": "Agent 인격, 어조, 성격",
        "user": "사용자 정보 및 선호도"
      },
      "uploadAvatar": "아바타 업로드",
      "crop": {
        "title": "아바타 자르기"
      },
      "role": {
        "charNote": "이름과 아바타 변경은 새 세션 생성 후에만 적용되며, 기존 세션에는 영향을 주지 않습니다. 역할 이름은 시스템 프롬프트에도 기록됩니다.",
        "noFileChosen": "새 아바타 이미지를 선택할 수 있습니다",
        "resetAvatar": "아바타 초기화"
      }
    }
  }
}
</i18n>

<style scoped>
/* The strip's own surface must wrap EVERY tab. The theme paints the background
   and the bottom border on the tab ROW, whose box is capped at the scroller's
   width — a row that overflows (four tabs are wider than a narrow sidebar) then
   scrolls its last tabs onto bare panel background, with the underline cut off
   at the row's edge. `max-content` grows the row with its tabs (so added tabs
   stay wrapped) and `min-width: 100%` keeps the underline spanning the panel
   when a tab is removed. */
:deep(.p-tabview-tablist) {
  width: max-content;
  min-width: 100%;
}

/* 让面板内容区成为 flex 列容器：根布局用 flex-1 精确填充内容区高度，
   内部 flex 链（TabView → panels → panel → textarea）自适应伸缩，
   避免 72vh 等固定高度把内容区撑出垂直滚动条。 */
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
