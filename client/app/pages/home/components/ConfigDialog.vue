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
      <div v-if="loading" class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 2rem; height: 2rem" />
      </div>
      <template v-else>
        <TabView v-model:activeIndex="activeTab">
          <TabPanel
            v-for="tab in tabs"
            :key="tab.file"
            :header="t(tab.i18nKey)">
            <div class="flex flex-col gap-2">
              <div class="flex items-center justify-between">
                <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
                <span
                  :class="[
                    'text-xs',
                    (editContent[tab.file]?.length ?? 0) > MAX_CHARS
                      ? 'text-red-500'
                      : (editContent[tab.file]?.length ?? 0) > MAX_CHARS * 0.9
                        ? 'text-orange-500'
                        : 'text-gray-400'
                  ]">
                  {{ editContent[tab.file]?.length ?? 0 }} / {{ MAX_CHARS }}
                </span>
              </div>
              <Textarea
                v-model="editContent[tab.file]"
                rows="12"
                class="w-full font-mono text-sm"
                :maxlength="MAX_CHARS"
                style="height: 72vh; min-height: 72vh; max-height: 72vh; overflow: auto" />
            </div>
          </TabPanel>
          <TabPanel :header="t('config.tabs.language')">
            <div class="flex flex-col gap-5">
              <div class="flex flex-col gap-2">
                <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{ t('config.language.label') }}</span>
                <div class="flex items-center gap-3">
                  <Select
                    :model-value="selectedLocale"
                    :options="languageOptions"
                    option-label="name"
                    option-value="code"
                    class="w-full md:w-64"
                    @update:model-value="(value: string) => (selectedLocale = value)" />
                </div>
              </div>
            </div>
          </TabPanel>
          <TabPanel :header="t('config.tabs.character')">
            <div class="flex flex-col gap-5">
              <!-- AI 角色配置 -->
              <div class="flex flex-col gap-2">
                <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{ t('config.role.assistant') }}</span>
                <div class="flex items-center gap-3">
                  <img
                    v-if="charAssistant.avatar"
                    :src="assistantAvatarUrl"
                    alt="assistant avatar"
                    class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                  <div v-else class="w-14 h-14 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center text-gray-400">
                    <i class="pi pi-user" />
                  </div>
                  <div class="flex flex-col gap-2 flex-1">
                    <InputText v-model="charAssistant.name" :placeholder="t('config.role.aiName')" class="w-full" />
                    <FileUpload
                      mode="basic"
                      :choose-label="t('config.uploadAvatar')"
                      accept="image/*"
                      customUpload
                      :auto="false"
                      @select="onAssistAvatarSelect" />
                  </div>
                </div>
              </div>

              <Divider />

              <!-- 用户角色配置 -->
              <div class="flex flex-col gap-2">
                <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{ t('config.role.userRole') }}</span>
                <div class="flex items-center gap-3">
                  <img
                    v-if="charUser.avatar"
                    :src="userAvatarUrl"
                    alt="user avatar"
                    class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                  <div v-else class="w-14 h-14 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center text-gray-400">
                    <i class="pi pi-user" />
                  </div>
                  <div class="flex flex-col gap-2 flex-1">
                    <InputText v-model="charUser.name" :placeholder="t('config.role.userName')" class="w-full" />
                    <FileUpload
                      mode="basic"
                      :choose-label="t('config.uploadAvatar')"
                      accept="image/*"
                      customUpload
                      :auto="false"
                      @select="onUserAvatarSelect" />
                  </div>
                </div>
              </div>
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
</template>

<script lang="ts" setup>
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { readSystemPrompt, writeSystemPrompt, readCharacter, updateCharacter, uploadAvatar } from '@/composables/bridge';

const { t, locale, setLocale } = useI18n();

const languageOptions = [
  { name: t('config.language.zh'), code: 'zh' },
  { name: t('config.language.en'), code: 'en' },
  { name: t('config.language.ja'), code: 'ja' },
  { name: t('config.language.ko'), code: 'ko' },
];

const selectedLocale = computed({
  get: () => locale.value,
  set: (value: string) => setLocale(value),
});

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emits('update:modelValue', v),
});

const MAX_CHARS = 2000;

const tabs = [
  { file: 'AGENTS.md', i18nKey: 'config.tabs.agents', i18nDescKey: 'config.desc.agents' },
  { file: 'IDENTITY.md', i18nKey: 'config.tabs.identity', i18nDescKey: 'config.desc.identity' },
  { file: 'SOUL.md', i18nKey: 'config.tabs.soul', i18nDescKey: 'config.desc.soul' },
  { file: 'USER.md', i18nKey: 'config.tabs.user', i18nDescKey: 'config.desc.user' },
] as const;

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
const editContent = ref<Record<string, string>>({});
const originalContent = ref<Record<string, string>>({});

// ── 角色配置状态 ─────────────────────────────────────────
const backendBaseUrl = (import.meta.env.VITE_API_BACK_URL as string) || 'http://localhost:8080';

const charUser = ref<{ name: string; avatar: string }>({ name: '', avatar: '' });
const charAssistant = ref<{ name: string; avatar: string }>({ name: '', avatar: '' });
const originalChar = ref<{ user: { name: string; avatar: string }; assistant: { name: string; avatar: string } }>({
  user: { name: '', avatar: '' },
  assistant: { name: '', avatar: '' },
});

const resolveStaticUrl = (path?: string) =>
  path ? `${backendBaseUrl.replace(/\/+$/, '')}/static/${path.replace(/^\/+/, '')}` : '';

const userAvatarUrl = computed(() => resolveStaticUrl(charUser.value.avatar));
const assistantAvatarUrl = computed(() => resolveStaticUrl(charAssistant.value.avatar));

const canSave = computed(() => {
  if (loading.value || saving.value) return false;
  const promptsValid = tabs.every(
    (tab) =>
      (editContent.value[tab.file]?.length ?? 0) <= MAX_CHARS &&
      (editContent.value[tab.file]?.length ?? 0) > 0,
  );
  const charValid = charUser.value.name.trim().length > 0 && charAssistant.value.name.trim().length > 0;
  return promptsValid && charValid;
});

// ── 头像上传处理 ─────────────────────────────────────────
const onUserAvatarSelect = async (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  try {
    const path = await uploadAvatar(file);
    charUser.value.avatar = path;
  } catch (e) {
    console.error('[ConfigDialog] Avatar upload failed (user):', e);
  }
};

const onAssistAvatarSelect = async (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  try {
    const path = await uploadAvatar(file);
    charAssistant.value.avatar = path;
  } catch (e) {
    console.error('[ConfigDialog] Avatar upload failed (assistant):', e);
  }
};

const loadContent = async () => {
  loading.value = true;
  try {
    const [promptData, charData] = await Promise.all([readSystemPrompt(), readCharacter()]);

    const content: Record<string, string> = {};
    for (const tab of tabs) {
      content[tab.file] = promptData[tab.file] ?? '';
    }
    editContent.value = { ...content };
    originalContent.value = { ...content };

    const user = charData?.user ?? {};
    const assistant = charData?.assistant ?? {};
    charUser.value = { name: user.name || '', avatar: user.avatar || '' };
    charAssistant.value = { name: assistant.name || '', avatar: assistant.avatar || '' };
    originalChar.value = {
      user: { name: user.name || '', avatar: user.avatar || '' },
      assistant: { name: assistant.name || '', avatar: assistant.avatar || '' },
    };
  } catch (e) {
    console.error('[ConfigDialog] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

const handleSave = async () => {
  saving.value = true;
  try {
    const fileToContent: Record<string, string> = {};
    for (const tab of tabs) {
      const content = editContent.value[tab.file] ?? '';
      if (content !== (originalContent.value[tab.file] ?? '')) {
        fileToContent[tab.file] = content;
      }
    }
    if (Object.keys(fileToContent).length > 0) {
      await writeSystemPrompt(fileToContent);
    }

    // 角色配置：仅在 name 或 avatar 有变更时提交更新（完整提交该 role）
    const charUpdate: Record<string, Record<string, string>> = {};
    if (
      charUser.value.name.trim().length > 0 &&
      (charUser.value.name !== originalChar.value.user.name || charUser.value.avatar !== originalChar.value.user.avatar)
    ) {
      charUpdate.user = {
        name: charUser.value.name,
        avatar: charUser.value.avatar,
      };
    }
    if (
      charAssistant.value.name.trim().length > 0 &&
      (charAssistant.value.name !== originalChar.value.assistant.name || charAssistant.value.avatar !== originalChar.value.assistant.avatar)
    ) {
      charUpdate.assistant = {
        name: charAssistant.value.name,
        avatar: charAssistant.value.avatar,
      };
    }
    if (Object.keys(charUpdate).length > 0) {
      await updateCharacter(charUpdate);
    }

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
};
</script>
