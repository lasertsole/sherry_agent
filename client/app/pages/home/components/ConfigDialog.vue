<template>
  <Dialog
    v-model:visible="visible"
    :header="t('config.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[1100px]"
    @show="loadContent"
    @hide="onHide">
    <div class="flex flex-col gap-3">
      <div
        v-if="loading"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 2rem; height: 2rem" />
      </div>
      <template v-else>
        <TabView v-model:activeIndex="activeTab">
          <TabPanel
            value="character"
            :header="t('config.tabs.character')">
            <div class="flex flex-col gap-5">
              <p class="m-0 text-xs font-medium text-red-600 dark:text-red-400">{{ t('config.role.charNote') }}</p>

              <!-- AI role configuration -->
              <div class="flex flex-col gap-2">
                <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{
                  t('config.role.assistant')
                }}</span>
                <div class="flex items-center gap-3">
                  <img
                    v-if="charAssistant.avatar"
                    :src="assistantAvatarUrl"
                    alt="assistant avatar"
                    class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                  <div
                    v-else
                    class="w-14 h-14 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center text-gray-400">
                    <i class="pi pi-user" />
                  </div>
                  <div class="flex flex-col gap-2 flex-1">
                    <InputText
                      v-model="charAssistant.name"
                      :placeholder="t('config.role.aiName')"
                      class="w-full" />
                    <FileUpload
                      mode="basic"
                      :choose-label="t('config.uploadAvatar')"
                      accept="image/*"
                      customUpload
                      :auto="false"
                      @select="onAssistAvatarSelect">
                      <!-- filelabel shows the browser-native "No file chosen" when no file is selected by default;
                           replaced with localized text: file selected → show the file name; otherwise → prompt to upload a new avatar -->
                      <template #filelabel="{ files }">
                        <span class="text-xs text-gray-400">
                          {{ avatarFileLabel(Array.isArray(files) ? files : []) }}
                        </span>
                      </template>
                    </FileUpload>
                  </div>
                </div>
              </div>

              <Divider />

              <!-- User role configuration -->
              <div class="flex flex-col gap-2">
                <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{
                  t('config.role.userRole')
                }}</span>
                <div class="flex items-center gap-3">
                  <img
                    v-if="charUser.avatar"
                    :src="userAvatarUrl"
                    alt="user avatar"
                    class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                  <div
                    v-else
                    class="w-14 h-14 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center text-gray-400">
                    <i class="pi pi-user" />
                  </div>
                  <div class="flex flex-col gap-2 flex-1">
                    <InputText
                      v-model="charUser.name"
                      :placeholder="t('config.role.userName')"
                      class="w-full" />
                    <FileUpload
                      mode="basic"
                      :choose-label="t('config.uploadAvatar')"
                      accept="image/*"
                      customUpload
                      :auto="false"
                      @select="onUserAvatarSelect">
                      <!-- filelabel shows the browser-native "No file chosen" when no file is selected by default;
                           replaced with localized text: file selected → show the file name; otherwise → prompt to upload a new avatar -->
                      <template #filelabel="{ files }">
                        <span class="text-xs text-gray-400">
                          {{ avatarFileLabel(Array.isArray(files) ? files : []) }}
                        </span>
                      </template>
                    </FileUpload>
                  </div>
                </div>
              </div>
            </div>
          </TabPanel>
          <TabPanel
            value="background"
            :header="t('config.background.title')">
            <div class="flex flex-col gap-5">
              <!-- Background image: shown in both light/dark themes; the slider below controls the themed overlay (light=white / dark=black) -->
              <div class="flex items-center justify-between gap-3">
                <p class="m-0 text-xs font-medium text-gray-500 dark:text-gray-400">
                  {{ t('config.background.bothThemes') }}
                </p>
                <Button
                  v-if="backgroundUrl"
                  :label="t('config.background.clear')"
                  icon="pi pi-times"
                  severity="secondary"
                  size="small"
                  @click="backgroundUrl = ''" />
              </div>

              <!-- Background preview: displayed at the window's aspect ratio; scrolls inside the container when the browser window is too short; a themed overlay is stacked on top to preview the slider effect in real time -->
              <div
                v-if="backgroundUrl"
                class="relative w-full rounded-lg border border-solid border-gray-300 dark:border-gray-700 overflow-y-auto"
                :style="{ aspectRatio: String(backgroundAspect), maxHeight: '60vh' }">
                <img
                  :src="backgroundUrl"
                  alt="chat background"
                  class="w-full h-full object-cover" />
                <div
                  class="absolute inset-0 pointer-events-none"
                  :style="backgroundPreviewOverlayStyle" />
              </div>
              <div
                v-else
                class="w-full rounded-lg border border-dashed border-gray-300 dark:border-gray-600 flex items-center justify-center text-gray-400"
                :style="{ aspectRatio: String(backgroundAspect) }">
                <i class="pi pi-image mr-2" />
                <span class="text-sm">{{ t('config.background.title') }}</span>
              </div>

              <div class="flex items-center gap-3">
                <FileUpload
                  mode="basic"
                  :choose-label="t('config.background.upload')"
                  accept="image/*"
                  customUpload
                  :auto="false"
                  @select="onBackgroundSelect">
                  <!-- filelabel shows the browser-native "No file chosen" when no file is selected by default;
                       replaced with localized text: file selected → show the file name; background already set → prompt that a background exists; otherwise → prompt to choose an image -->
                  <template #filelabel="{ files }">
                    <span class="text-xs text-gray-400">
                      {{ fileLabelText(Array.isArray(files) ? files : []) }}
                    </span>
                  </template>
                </FileUpload>
              </div>

              <!-- Overlay opacity: light theme=white overlay / dark theme=black overlay; the further left, the clearer the photo; the further right, the more it fades until fully covered by pure white/black -->
              <div
                v-if="backgroundUrl"
                class="flex flex-col gap-2">
                <label class="text-sm font-medium text-[#111827] dark:text-[#E5E7EB]">
                  {{ t('config.background.opacity') }}
                  <span class="ml-1 text-xs text-gray-400">({{ backgroundOpacityValue }})</span>
                </label>
                <Slider
                  v-model="backgroundOpacityValue"
                  :min="0"
                  :max="100"
                  :step="5"
                  class="w-full" />
                <p class="m-0 text-xs text-gray-400">
                  {{ t('config.background.opacityHint') }}
                </p>
              </div>
            </div>
          </TabPanel>

          <!-- Env config tab: reads/edits the project root .env, grouped by prefix; only existing keys can be modified.
               Loading is driven by the setup-scoped watch below (@show runs in an event context where
               getCurrentInstance() is null, so Nuxt useFetch never actually sends the request) -->
          <TabPanel
            value="env"
            :header="t('config.tabs.env')">
            <div class="flex flex-col gap-4">
              <p class="m-0 text-xs font-medium text-gray-500 dark:text-gray-400">
                {{ t('config.env.restartHint') }}
              </p>

              <div
                v-if="envLoadError"
                class="flex">
                <p class="m-0 text-sm text-red-600 dark:text-red-400">{{ envLoadError }}</p>
              </div>

              <template v-else-if="envGroups.length === 0">
                <p class="m-0 text-sm text-gray-400">{{ t('config.env.noEnvFile') }}</p>
              </template>

              <template v-else>
                <div
                  v-for="group in envGroups"
                  :key="group.name"
                  class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
                  <p class="m-0 text-xs font-semibold text-gray-500 dark:text-gray-400">
                    {{ group.name }}
                  </p>
                  <div
                    v-for="entry in group.entries"
                    :key="entry.key"
                    class="flex flex-col gap-1">
                    <span class="text-xs text-gray-500 dark:text-gray-400">{{ entry.key }}</span>
                    <InputText
                      v-model="entry.value"
                      :class="entry.value !== originalEnvValues[entry.key] ? 'border-amber-400' : ''"
                      class="w-full font-mono text-xs"
                      autocomplete="off"
                      spellcheck="false" />
                  </div>
                </div>
              </template>
            </div>
          </TabPanel>
        </TabView>
      </template>
    </div>
    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button
          :label="t('config.cancel')"
          icon="pi pi-times"
          severity="secondary"
          @click="visible = false" />
        <Button
          :label="t('config.save')"
          icon="pi pi-check"
          :loading="saving"
          :disabled="!canSave"
          @click="handleSave" />
      </div>
    </template>
  </Dialog>

  <AvatarCropDialog
    v-model="cropVisible"
    :src="cropSource"
    :aspect-ratio="cropAspectRatio"
    :output-width="cropOutput.width"
    :output-height="cropOutput.height"
    :header="cropVisible ? cropTitle : ''"
    @cropped="onCropConfirmed" />
</template>

<script lang="ts" setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  GLOBAL_SESSION_KEY,
  DEFAULT_CACHED_CHARACTER,
  readCachedCharacter,
  cacheCharacter,
  readBackgroundConfig
} from '@/composables/db';
import AvatarCropDialog from './AvatarCropDialog.vue';
import type { EnvGroup } from '@/composables/env';
import { readEnvConfig, writeEnvConfig } from '@/composables/env';

/** Global chat-area background singleton: setBackground updates the reactive state and persists it synchronously, taking effect immediately after save */
const { backgroundOpacity, setBackground } = useChatBackground();

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);

// ── Env config state (.env) ──────────────────────────────────
// Snapshot taken once on load; edits directly modify entry.value inside envGroups, and on save the changes are diffed against the snapshot and written back to the backend.
const envGroups = ref<EnvGroup[]>([]);
const originalEnvValues = ref<Record<string, string>>({});
const envLoadError = ref('');

/** Whether the env config has already been loaded (avoids duplicate GETs) */
const envLoaded = ref(false);

/** Computes whether the env config has changes (used for canSave and save decisions) */
const envHasChanges = computed(() =>
  envGroups.value.some(group => group.entries.some(entry => entry.value !== originalEnvValues.value[entry.key]))
);

/** Lazily loads the .env config when the env tab is opened (backend GET /env) */
const loadEnvConfig = async () => {
  if (envLoaded.value) return;
  envLoaded.value = true;
  envLoadError.value = '';
  try {
    const payload = await readEnvConfig();
    envGroups.value = payload.groups || [];
    const snap: Record<string, string> = {};
    for (const g of envGroups.value) {
      for (const e of g.entries) snap[e.key] = e.value;
    }
    originalEnvValues.value = snap;
  } catch (e) {
    console.error('[ConfigDialog] Failed to load env config:', e);
    envLoadError.value = t('config.env.loadError');
    envLoaded.value = false;
  }
};

/** Writes env changes back to the backend (.env PUT); returns true on success */
const persistEnvChanges = async (): Promise<boolean> => {
  const changes: Record<string, string> = {};
  for (const g of envGroups.value) {
    for (const e of g.entries) {
      if (e.value !== originalEnvValues.value[e.key]) changes[e.key] = e.value;
    }
  }
  if (Object.keys(changes).length === 0) return true;
  const ok = await writeEnvConfig(changes);
  if (ok) {
    // Sync the snapshot to serve as the baseline for the next diff
    for (const g of envGroups.value) {
      for (const e of g.entries) originalEnvValues.value[e.key] = e.value;
    }
  }
  return ok;
};

/** Resets the env tab every time the dialog hides (cancel or save): reloads on next open */
const resetEnvState = () => {
  envGroups.value = [];
  originalEnvValues.value = {};
  envLoadError.value = '';
  envLoaded.value = false;
};

// ── Env config load trigger (setup scope) ──────────────
// Previously loadEnvConfig was called from the PrimeVue TabPanel @show event: event callbacks run in a
// non-setup context where getCurrentInstance() is null, so Nuxt useFetch(server:true) never sent a request
// in pure SPA mode and data stayed undefined forever → the || { groups: [] } fallback kicked in and rendered
// the misleading "No .env file found" message.
// Instead, dialog visibility + the env tab (activeTab===2) are now watched in setup scope; the callback runs
// in a setup context where getCurrentInstance() stays alive → useFetch actually issues GET /env and loads the real .env groups.
watch(
  [() => props.modelValue, activeTab],
  ([dialogVisible, tab]) => {
    if (dialogVisible && tab === 2) void loadEnvConfig();
  },
  // The dialog goes hidden→visible via v-model, so no immediate trigger is needed; resetEnvState already resets envLoaded on hide
  { flush: 'post' }
);

// ── Character config state ─────────────────────────────────────
// Character avatar/name are saved entirely locally on the frontend: written to the global pending profile in Dexie (the GLOBAL_SESSION_KEY row).
// The avatar can be a base64 data URL (`data:image/...;base64,...`, user-defined) or a `/avatar/xxx.jpg`
// relative URL (built-in default); both render directly in `<img>`.
// Saving only updates the global profile and never touches the snapshots already locked in per session → only new sessions pick up the new values.

const charUser = ref<{ name: string; avatar: string }>({
  name: DEFAULT_CACHED_CHARACTER.userName,
  avatar: DEFAULT_CACHED_CHARACTER.userAvatar
});
const charAssistant = ref<{ name: string; avatar: string }>({
  name: DEFAULT_CACHED_CHARACTER.aiName,
  avatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});
const originalChar = ref<{ user: { name: string; avatar: string }; assistant: { name: string; avatar: string } }>({
  user: { name: DEFAULT_CACHED_CHARACTER.userName, avatar: DEFAULT_CACHED_CHARACTER.userAvatar },
  assistant: { name: DEFAULT_CACHED_CHARACTER.aiName, avatar: DEFAULT_CACHED_CHARACTER.aiAvatar }
});

// The avatar is already a full image address (base64 data URL or /avatar/xxx.jpg relative URL) and renders directly (no need to prepend a static/ path)
const userAvatarUrl = computed(() => charUser.value.avatar);
const assistantAvatarUrl = computed(() => charAssistant.value.avatar);

/** Reads an uploaded image file as a base64 data URL */
const readFileAsDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

const canSave = computed(() => {
  if (loading.value || saving.value) return false;
  // Tab 2: env config; saveable only when there are changes
  if (activeTab.value === 2) return envHasChanges.value;
  // Tab 0: character config — both character names must be non-empty
  if (activeTab.value === 0) {
    return charUser.value.name.trim().length > 0 && charAssistant.value.name.trim().length > 0;
  }
  // Tab 1: background — no extra validation needed
  return true;
});

// ── Image crop handling (reuses AvatarCropDialog: avatars 1:1, background adapted to the screen) ──
// After an image is selected, the crop dialog opens: avatars are forced to a 1:1 square (512×512);
// the background is cropped and output at the **actual aspect ratio of the current chat window/screen** so it fits any ratio (16:9, 16:10, 3:2, 21:9…);
// and since rendering uses `background-size: cover`, edges get cut off under cover unless the crop ratio == the window ratio.
// The ratio/size are **snapshotted** once at the moment the dialog opens (avoids the crop box jumping while the window is being dragged).
const cropVisible = ref(false);
const cropSource = ref('');
const cropTarget = ref<'user' | 'assistant' | 'background'>('user');

/** Crop box aspect ratio and output size (snapshotted when the crop box opens; avatars fixed at 1:1) */
const cropAspectRatio = ref(1);
const cropOutput = ref({ width: 512, height: 512 });

/**
 * Background crop size: generated from the current window's physical pixels (aspect ratio == chat window aspect ratio).
 * Based on the window's real physical resolution (logical width/height × devicePixelRatio) so any screen gets a sharp full fill.
 */
const getBackgroundCrop = () => {
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  const innerW = (typeof window !== 'undefined' && window.innerWidth) || 1920;
  const innerH = (typeof window !== 'undefined' && window.innerHeight) || 1080;
  const w = Math.max(512, Math.round(innerW * dpr));
  const h = Math.max(288, Math.round(innerH * dpr));
  return { width: w, height: h };
};
/** Crop dialog title */
const cropTitle = computed(() =>
  cropTarget.value === 'background' ? t('config.background.cropTitle') : t('config.crop.title')
);

const onUserAvatarSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  openCrop('user', file);
};

const onAssistAvatarSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  openCrop('assistant', file);
};

const openCrop = async (target: 'user' | 'assistant' | 'background', file: File) => {
  try {
    cropTarget.value = target;
    // Snapshot the crop ratio/output size when the dialog opens: background adapts to the current window ratio, avatar fixed square
    if (target === 'background') {
      const { width, height } = getBackgroundCrop();
      cropAspectRatio.value = width / height;
      cropOutput.value = { width, height };
    } else {
      cropAspectRatio.value = 1;
      cropOutput.value = { width: 512, height: 512 };
    }
    cropSource.value = await readFileAsDataUrl(file);
    cropVisible.value = true;
  } catch (e) {
    console.error('[ConfigDialog] Image read failed:', e);
  }
};

const onCropConfirmed = (dataUrl: string) => {
  if (cropTarget.value === 'background') {
    backgroundUrl.value = dataUrl;
  } else if (cropTarget.value === 'user') {
    charUser.value.avatar = dataUrl;
  } else {
    charAssistant.value.avatar = dataUrl;
  }
  cropVisible.value = false;
};

// ── Background settings state ────────────────────────────────
// The chat-area background image is saved locally on the frontend only: written to the single global row in Dexie (GLOBAL_SESSION_KEY),
// for the main chat page to read and render under light/dark themes (photo + themed overlay).
const backgroundUrl = ref('');
const originalBackgroundUrl = ref('');
/** Overlay opacity (0-100 integer, light=white / dark=black). Local edit state: snapshotted from the singleton when the dialog opens, written back on save */
const backgroundOpacityValue = ref(0);

const colorMode = useColorMode();

/**
 * Preview overlay style: identical to the real chat-area overlay — light=white / dark=black, with opacity tracking the slider in real time,
 * letting users see the true effect of "dragging the slider fades the photo to white/black" right on the preview image.
 */
const backgroundPreviewOverlayStyle = computed(() => {
  const overlayColor = colorMode.value === 'light' ? '#ffffff' : '#000000';
  return {
    backgroundColor: overlayColor,
    opacity: (backgroundOpacityValue.value || 0) / 100
  };
});

/** Reactive window size (window is not reactive, so a ref + resize listener drives the placeholder box's aspect ratio to follow window changes) */
const windowSize = ref(getWindowSize());
function getWindowSize() {
  if (typeof window === 'undefined') return { width: 1920, height: 1080 };
  return { width: window.innerWidth, height: window.innerHeight };
}
function onWindowResize() {
  windowSize.value = getWindowSize();
}
onMounted(() => window.addEventListener('resize', onWindowResize));
onBeforeUnmount(() => window.removeEventListener('resize', onWindowResize));

/** Empty-state background placeholder box aspect ratio == current window aspect ratio (matches the crop preview for an intuitive look at the chat background ratio) */
const backgroundAspect = computed(() => {
  const innerW = windowSize.value.width;
  const innerH = windowSize.value.height;
  return Number((innerW / innerH).toFixed(4));
});

/** Opens the crop dialog after a background image is selected (16:9, reuses the avatar crop UI); only the cropped result becomes the background */
const onBackgroundSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  openCrop('background', file);
};

/**
 * Localized label for the background FileUpload (`#filelabel` slot, replacing the browser-native "No file chosen"):
 * file selected → show the file name; background already set → prompt that a background exists; otherwise → prompt to upload a new background.
 */
const fileLabelText = (files: File[]): string => {
  if (files.length > 0) return files[0]?.name ?? '';
  if (backgroundUrl.value) return t('config.background.current');
  return t('config.background.noFileChosen');
};

/**
 * Localized label for the avatar FileUpload (`#filelabel` slot, replacing the browser-native "No file chosen"):
 * file selected → show the file name; otherwise → prompt to upload a new avatar.
 */
const avatarFileLabel = (files: File[]): string => {
  if (files.length > 0) return files[0]?.name ?? '';
  return t('config.role.noFileChosen');
};

const loadContent = async () => {
  loading.value = true;
  try {
    const charData = await readCachedCharacter(GLOBAL_SESSION_KEY);

    // Read character config from the local Dexie global profile (falls back to the built-in defaults when no record exists: Tono Hanna / Tachibana Sherry + default avatars)
    charUser.value = {
      name: charData?.userName?.trim() ? charData.userName : DEFAULT_CACHED_CHARACTER.userName,
      avatar: charData?.userAvatar ?? DEFAULT_CACHED_CHARACTER.userAvatar
    };
    charAssistant.value = {
      name: charData?.aiName?.trim() ? charData.aiName : DEFAULT_CACHED_CHARACTER.aiName,
      avatar: charData?.aiAvatar ?? DEFAULT_CACHED_CHARACTER.aiAvatar
    };
    originalChar.value = {
      user: { name: charUser.value.name, avatar: charUser.value.avatar },
      assistant: { name: charAssistant.value.name, avatar: charAssistant.value.avatar }
    };

    // Read the global background config from local Dexie (falls back to empty string + opacity 0 when unset)
    const bgConfig = (await readBackgroundConfig()) ?? { backgroundUrl: '', backgroundOpacity: 0 };
    backgroundUrl.value = bgConfig.backgroundUrl;
    originalBackgroundUrl.value = bgConfig.backgroundUrl;
    backgroundOpacityValue.value = bgConfig.backgroundOpacity;
  } catch (e) {
    console.error('[ConfigDialog] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

const handleSave = async () => {
  saving.value = true;
  try {
    // Character config: only when name or avatar changed, write the changes to the local Dexie global profile.
    // This write only affects the global profile and never touches the snapshots locked in per session → only affects new sessions.
    const userChanged =
      charUser.value.name !== originalChar.value.user.name || charUser.value.avatar !== originalChar.value.user.avatar;
    const assistantChanged =
      charAssistant.value.name !== originalChar.value.assistant.name ||
      charAssistant.value.avatar !== originalChar.value.assistant.avatar;
    if (userChanged || assistantChanged) {
      const existing = (await readCachedCharacter(GLOBAL_SESSION_KEY)) ?? {
        session_id: GLOBAL_SESSION_KEY,
        userName: '',
        userAvatar: '',
        aiName: '',
        aiAvatar: ''
      };
      await cacheCharacter({
        session_id: GLOBAL_SESSION_KEY,
        userName: userChanged ? charUser.value.name : existing.userName,
        userAvatar: userChanged ? charUser.value.avatar : existing.userAvatar,
        aiName: assistantChanged ? charAssistant.value.name : existing.aiName,
        aiAvatar: assistantChanged ? charAssistant.value.avatar : existing.aiAvatar
      });
      // After a successful save, sync originalChar to serve as the baseline for the next diff
      originalChar.value = {
        user: { name: charUser.value.name, avatar: charUser.value.avatar },
        assistant: { name: charAssistant.value.name, avatar: charAssistant.value.avatar }
      };
    }

    // Background image: only when changed, write to the local Dexie global row (empty string means clearing the background).
    // setBackground/setBackgroundOpacity synchronously update the shared singleton's reactive state, so the root container's background + overlay take effect immediately without a refresh.
    const bgUrlChanged = backgroundUrl.value !== originalBackgroundUrl.value;
    const bgOpacityChanged = backgroundOpacityValue.value !== backgroundOpacity.value;
    if (bgUrlChanged || bgOpacityChanged) {
      await setBackground(backgroundUrl.value, backgroundOpacityValue.value);
      originalBackgroundUrl.value = backgroundUrl.value;
    }

    // Env config: if currently on the env tab and there are changes, write them back to the backend .env. Abort on save failure without closing the dialog.
    if (activeTab.value === 2 && envHasChanges.value) {
      const ok = await persistEnvChanges();
      if (!ok) {
        envLoadError.value = t('config.env.saveFailed');
        return;
      }
    }

    // Discard the env edit state after the dialog closes (resetEnvState in onHide) so the .env is re-read on next open
    emits('saved');
    visible.value = false;
  } catch (e) {
    console.error('[ConfigDialog] Failed to save:', e);
  } finally {
    saving.value = false;
  }
};

const onHide = () => {
  activeTab.value = 0;
  backgroundOpacityValue.value = backgroundOpacity.value;
  // Env config changes are kept only after a successful save; canceling / closing on a non-env tab always discards them → reloaded on next open
  resetEnvState();
};
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "title": "系统配置",
      "uploadAvatar": "上传头像",
      "role": {
        "assistant": "AI 角色",
        "aiName": "AI 名称",
        "userRole": "用户角色",
        "userName": "用户名称",
        "charNote": "修改头像与名字仅在新建会话后生效，旧会话不受影响。",
        "noFileChosen": "可选择新的头像图片"
      },
      "background": {
        "title": "背景图片",
        "upload": "上传背景",
        "clear": "清除背景",
        "bothThemes": "背景图片在浅色/深色主题下均会显示；通过下方滑块调整遮罩强度。",
        "opacity": "遮罩强度",
        "opacityHint": "浅色主题叠加白色遮罩、深色主题叠加黑色遮罩：越靠右照片越被冲淡成纯白/纯黑，直至完全遮蔽。",
        "cropTitle": "裁剪背景",
        "current": "已设置背景图",
        "noFileChosen": "可选择新图片文件"
      },
      "env": {
        "loadError": "环境配置加载失败，请检查后端服务是否已启动。",
        "saveFailed": "环境配置保存失败，请检查 key 与值是否合法。",
        "restartHint": "修改 API Key 等敏感配置后，需重启后端服务才能生效。",
        "noEnvFile": "未找到 .env 文件。"
      }
    }
  },
  "en": {
    "config": {
      "title": "System Config",
      "uploadAvatar": "Upload Avatar",
      "role": {
        "assistant": "AI Role",
        "aiName": "AI Name",
        "userRole": "User Role",
        "userName": "User Name",
        "charNote": "Changes to the avatar and name only take effect in new sessions; existing sessions are not affected.",
        "noFileChosen": "Select an avatar image file"
      },
      "background": {
        "title": "Background Image",
        "upload": "Upload Background",
        "clear": "Clear Background",
        "bothThemes": "The background image shows in both light and dark themes; adjust the overlay strength with the slider below.",
        "opacity": "Overlay Strength",
        "opacityHint": "A white overlay is used in light theme, black in dark theme: the further right, the more the photo fades to solid white/black until fully covered.",
        "cropTitle": "Crop Background",
        "current": "Background image set",
        "noFileChosen": "Select an image file"
      },
      "env": {
        "loadError": "Failed to load environment config. Please check the backend service.",
        "saveFailed": "Failed to save environment config.",
        "restartHint": "After changing sensitive values (e.g. API keys), restart the backend service for the changes to take effect.",
        "noEnvFile": "No .env file found."
      }
    }
  },
  "ja": {
    "config": {
      "title": "システム設定",
      "uploadAvatar": "アバターをアップロード",
      "role": {
        "assistant": "AI ロール",
        "aiName": "AI 名前",
        "userRole": "ユーザーロール",
        "userName": "ユーザー名",
        "charNote": "アバターと名前の変更は新しいセッション作成後にのみ反映され、既存のセッションには影響しません。",
        "noFileChosen": "新しいアバター画像を選択できます"
      },
      "background": {
        "title": "背景画像",
        "upload": "背景をアップロード",
        "clear": "背景をクリア",
        "bothThemes": "背景画像はライト/ダークテーマの両方で表示されます。下のスライダーでオーバーレイの強さを調整します。",
        "opacity": "オーバーレイの強さ",
        "opacityHint": "ライトテーマでは白、ダークテーマでは黒のオーバーレイを重ねます。右に行くほど写真が真っ白/真っ黒に薄れ、完全に覆われます。",
        "cropTitle": "背景をトリミング",
        "current": "背景画像が設定されています",
        "noFileChosen": "画像ファイルを選択"
      },
      "env": {
        "loadError": "環境設定の読み込みに失敗しました。バックエンドサービスを確認してください。",
        "saveFailed": "環境設定の保存に失敗しました。",
        "restartHint": "APIキーなどの機密設定を変更した場合、反映にはバックエンドの再起動が必要です。",
        "noEnvFile": ".env ファイルが見つかりません。"
      }
    }
  },
  "ko": {
    "config": {
      "title": "시스템 설정",
      "uploadAvatar": "아바타 업로드",
      "role": {
        "assistant": "AI 역할",
        "aiName": "AI 이름",
        "userRole": "사용자 역할",
        "userName": "사용자 이름",
        "charNote": "아바타와 이름 변경은 새 세션 생성 후에만 적용되며, 기존 세션에는 영향을 주지 않습니다.",
        "noFileChosen": "새 아바타 이미지를 선택할 수 있습니다"
      },
      "background": {
        "title": "배경 이미지",
        "upload": "배경 업로드",
        "clear": "배경 지우기",
        "bothThemes": "배경 이미지는 라이트/다크 테마 모두에서 표시됩니다. 아래 슬라이더로 오버레이 강도를 조정하세요.",
        "opacity": "오버레이 강도",
        "opacityHint": "라이트 테마는 흰색, 다크 테마는 검은색 오버레이를 덮습니다. 오른쪽으로 갈수록 사진이 순백/순흑으로 바래다 완전히 가려집니다.",
        "cropTitle": "배경 자르기",
        "current": "배경 이미지가 설정됨",
        "noFileChosen": "이미지 파일을 선택하세요"
      },
      "env": {
        "loadError": "환경 설정을 불러오지 못했습니다. 백엔드 서비스를 확인하세요.",
        "saveFailed": "환경 설정을 저장하지 못했습니다.",
        "restartHint": "API 키 등 민감한 설정을 변경한 경우, 적용하려면 백엔드를 재시작해야 합니다.",
        "noEnvFile": ".env 파일을 찾을 수 없습니다."
      }
    }
  }
}
</i18n>
