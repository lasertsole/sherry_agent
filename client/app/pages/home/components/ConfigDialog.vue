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
          <TabPanel :header="t('config.tabs.character')">
            <div class="flex flex-col gap-5">
              <p class="m-0 text-xs font-medium text-red-600 dark:text-red-400">{{ t('config.role.charNote') }}</p>

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
import { readSystemPrompt, writeSystemPrompt } from '@/composables/bridge';
import {
  GLOBAL_SESSION_KEY,
  DEFAULT_CACHED_CHARACTER,
  readCachedCharacter,
  cacheCharacter,
} from '@/composables/db';

const { t } = useI18n();

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
// 角色头像/名字完全由前端本地保存：写入 Dexie 的全局待定 profile（GLOBAL_SESSION_KEY 行）。
// 头像可为 base64 data URL（`data:image/...;base64,...`，用户自定义）或 `/avatar/xxx.jpg`
// 相对 URL（内置默认）；两者 `<img>` 均可直接渲染。
// 保存只更新全局 profile，不触碰各会话已锁定的快照 → 仅新会话取到新值。

const charUser = ref<{ name: string; avatar: string }>({
  name: DEFAULT_CACHED_CHARACTER.userName,
  avatar: DEFAULT_CACHED_CHARACTER.userAvatar,
});
const charAssistant = ref<{ name: string; avatar: string }>({
  name: DEFAULT_CACHED_CHARACTER.aiName,
  avatar: DEFAULT_CACHED_CHARACTER.aiAvatar,
});
const originalChar = ref<{ user: { name: string; avatar: string }; assistant: { name: string; avatar: string } }>({
  user: { name: DEFAULT_CACHED_CHARACTER.userName, avatar: DEFAULT_CACHED_CHARACTER.userAvatar },
  assistant: { name: DEFAULT_CACHED_CHARACTER.aiName, avatar: DEFAULT_CACHED_CHARACTER.aiAvatar },
});

// 头像已是完整图片地址（base64 data URL 或 /avatar/xxx.jpg 相对 URL），直接渲染（无需拼接 static/ 路径）
const userAvatarUrl = computed(() => charUser.value.avatar);
const assistantAvatarUrl = computed(() => charAssistant.value.avatar);

/** 将上传的图片文件读取为 base64 data URL */
const readFileAsDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

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

// ── 头像上传处理（本地转 base64，不调用后端） ──────────
const onUserAvatarSelect = async (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  try {
    charUser.value.avatar = await readFileAsDataUrl(file);
  } catch (e) {
    console.error('[ConfigDialog] Avatar read failed (user):', e);
  }
};

const onAssistAvatarSelect = async (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  try {
    charAssistant.value.avatar = await readFileAsDataUrl(file);
  } catch (e) {
    console.error('[ConfigDialog] Avatar read failed (assistant):', e);
  }
};

const loadContent = async () => {
  loading.value = true;
  try {
    const [promptData, charData] = await Promise.all([readSystemPrompt(), readCachedCharacter(GLOBAL_SESSION_KEY)]);

    const content: Record<string, string> = {};
    for (const tab of tabs) {
      content[tab.file] = promptData[tab.file] ?? '';
    }
    editContent.value = { ...content };
    originalContent.value = { ...content };

    // 从本地 Dexie 全局 profile 读取角色配置（无记录时回退到内置默认值：远野汉娜/橘雪莉 + 默认头像）
    charUser.value = {
      name: charData?.userName?.trim() ? charData.userName : DEFAULT_CACHED_CHARACTER.userName,
      avatar: charData?.userAvatar ?? DEFAULT_CACHED_CHARACTER.userAvatar,
    };
    charAssistant.value = {
      name: charData?.aiName?.trim() ? charData.aiName : DEFAULT_CACHED_CHARACTER.aiName,
      avatar: charData?.aiAvatar ?? DEFAULT_CACHED_CHARACTER.aiAvatar,
    };
    originalChar.value = {
      user: { name: charUser.value.name, avatar: charUser.value.avatar },
      assistant: { name: charAssistant.value.name, avatar: charAssistant.value.avatar },
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

    // 角色配置：仅当 name 或 avatar 有变更时，把变更写入本地 Dexie 全局 profile。
    // 该写入只影响全局 profile，不触碰各会话已锁定的快照 → 只影响新会话。
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
        aiAvatar: '',
      };
      await cacheCharacter({
        session_id: GLOBAL_SESSION_KEY,
        userName: userChanged ? charUser.value.name : existing.userName,
        userAvatar: userChanged ? charUser.value.avatar : existing.userAvatar,
        aiName: assistantChanged ? charAssistant.value.name : existing.aiName,
        aiAvatar: assistantChanged ? charAssistant.value.avatar : existing.aiAvatar,
      });
      // 保存成功后再同步 originalChar 作为下一次 diff 基线
      originalChar.value = {
        user: { name: charUser.value.name, avatar: charUser.value.avatar },
        assistant: { name: charAssistant.value.name, avatar: charAssistant.value.avatar },
      };
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
