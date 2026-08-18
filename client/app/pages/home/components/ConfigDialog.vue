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
                      @select="onAssistAvatarSelect" />
                  </div>
                </div>
              </div>

              <Divider />

              <!-- 用户角色配置 -->
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
                      @select="onUserAvatarSelect" />
                  </div>
                </div>
              </div>
            </div>
          </TabPanel>
          <TabPanel :header="t('config.background.title')">
            <div class="flex flex-col gap-5">
              <!-- 背景图：浅色/深色主题下均显示；下方 slider 控制主题化遮罩（浅色=白/深色=黑） -->
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

              <!-- 背景预览：按窗口长宽比展示，浏览器过矮时容器内滚动；叠加主题化遮罩以实时预览 slider 效果 -->
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
                  @select="onBackgroundSelect" />
              </div>

              <!-- 遮罩透明度：浅色=白色遮罩 / 深色=黑色遮罩；越向左照片越清晰，越向右越被冲淡直至纯白/纯黑遮蔽 -->
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
    :header="cropTitle"
    @cropped="onCropConfirmed" />
</template>

<script lang="ts" setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { readSystemPrompt, writeSystemPrompt } from '@/composables/bridge';
import {
  GLOBAL_SESSION_KEY,
  DEFAULT_CACHED_CHARACTER,
  readCachedCharacter,
  cacheCharacter,
  readBackgroundConfig
} from '@/composables/db';
import AvatarCropDialog from './AvatarCropDialog.vue';

/** 全局聊天区背景单例：setBackground 同步更新响应式状态并持久化，保存后立即生效 */
const { backgroundOpacity, setBackground } = useChatBackground();

const { t } = useI18n();

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const MAX_CHARS = 2000;

const tabs = [
  { file: 'AGENTS.md', i18nKey: 'config.tabs.agents', i18nDescKey: 'config.desc.agents' },
  { file: 'IDENTITY.md', i18nKey: 'config.tabs.identity', i18nDescKey: 'config.desc.identity' },
  { file: 'SOUL.md', i18nKey: 'config.tabs.soul', i18nDescKey: 'config.desc.soul' },
  { file: 'USER.md', i18nKey: 'config.tabs.user', i18nDescKey: 'config.desc.user' }
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
    tab => (editContent.value[tab.file]?.length ?? 0) <= MAX_CHARS && (editContent.value[tab.file]?.length ?? 0) > 0
  );
  const charValid = charUser.value.name.trim().length > 0 && charAssistant.value.name.trim().length > 0;
  return promptsValid && charValid;
});

// ── 图片裁剪处理（复用 AvatarCropDialog：头像 1:1，背景适配电脑屏幕） ──
// 选中图片后打开裁剪对话框：头像强制 1:1 正方形（512×512）；
// 背景按**当前聊天窗口/屏幕的真实长宽比**裁剪与输出，从而适配任何比例（16:9、16:10、3:2、21:9…），
// 且因渲染用 `background-size: cover`，只有裁剪比例 == 窗口比例才不会在 cover 时被裁掉边缘。
// 比例/尺寸在打开对话框那一刻**快照**一次（避免拖动窗口时裁剪框跳变）。
const cropVisible = ref(false);
const cropSource = ref('');
const cropTarget = ref<'user' | 'assistant' | 'background'>('user');

/** 裁剪框宽高比与输出尺寸（打开裁剪框时快照；头像固定 1:1） */
const cropAspectRatio = ref(1);
const cropOutput = ref({ width: 512, height: 512 });

/**
 * 背景裁剪尺寸：按当前窗口物理像素生成（宽高比 == 聊天窗口宽高比）。
 * 以窗口真实物理分辨率（逻辑宽高 × devicePixelRatio）为基准，保证任何屏幕都高清铺满。
 */
const getBackgroundCrop = () => {
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  const innerW = (typeof window !== 'undefined' && window.innerWidth) || 1920;
  const innerH = (typeof window !== 'undefined' && window.innerHeight) || 1080;
  const w = Math.max(512, Math.round(innerW * dpr));
  const h = Math.max(288, Math.round(innerH * dpr));
  return { width: w, height: h };
};
/** 裁剪对话框标题 */
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
    // 打开对话框时快照裁剪比例/输出尺寸：背景适配当前窗口比例，头像固定正方形
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

// ── 背景设置状态 ─────────────────────────────────────────
// 聊天区背景图仅前端本地保存：写入 Dexie 全局唯一行（GLOBAL_SESSION_KEY），
// 供主聊天页在浅色/深色主题下读取渲染（照片+主题化遮罩）。
const backgroundUrl = ref('');
const originalBackgroundUrl = ref('');
/** 遮罩透明度（0-100 整数，浅色=白/深色=黑）。本地编辑态，打开对话框时从单例快照，保存时写回 */
const backgroundOpacityValue = ref(0);

const colorMode = useColorMode();

/**
 * 预览遮罩样式：与聊天区真实遮罩一致——浅色=白 / 深色=黑，opacity 随 slider 实时变化，
 * 让用户在预览大图上即可看到「拉动滑块照片被冲淡成白/黑」的真实效果。
 */
const backgroundPreviewOverlayStyle = computed(() => {
  const overlayColor = colorMode.value === 'light' ? '#ffffff' : '#000000';
  return {
    backgroundColor: overlayColor,
    opacity: (backgroundOpacityValue.value || 0) / 100
  };
});

/** 响应式窗口尺寸（window 非响应式，故用 ref + resize 监听驱动占位框宽高比跟随窗口变化） */
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

/** 背景图片空态占位框宽高比 == 当前窗口宽高比（与裁剪预览一致，直观预览聊天背景比例） */
const backgroundAspect = computed(() => {
  const innerW = windowSize.value.width;
  const innerH = windowSize.value.height;
  return Number((innerW / innerH).toFixed(4));
});

/** 选中背景图片后打开裁剪对话框（16:9，复用头像裁剪 UI），仅裁剪结果作为背景图 */
const onBackgroundSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  openCrop('background', file);
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

    // 从本地 Dexie 读取全局背景配置（未设置则回退为空字符串 + 透明度 0）
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
        aiAvatar: ''
      };
      await cacheCharacter({
        session_id: GLOBAL_SESSION_KEY,
        userName: userChanged ? charUser.value.name : existing.userName,
        userAvatar: userChanged ? charUser.value.avatar : existing.userAvatar,
        aiName: assistantChanged ? charAssistant.value.name : existing.aiName,
        aiAvatar: assistantChanged ? charAssistant.value.avatar : existing.aiAvatar
      });
      // 保存成功后再同步 originalChar 作为下一次 diff 基线
      originalChar.value = {
        user: { name: charUser.value.name, avatar: charUser.value.avatar },
        assistant: { name: charAssistant.value.name, avatar: charAssistant.value.avatar }
      };
    }

    // 背景图：仅当有变更时写入本地 Dexie 全局行（空字符串表示清除背景）。
    // setBackground/setBackgroundOpacity 会同步更新共享单例的响应式 state，根容器背景+遮罩立即生效，无需刷新。
    const bgUrlChanged = backgroundUrl.value !== originalBackgroundUrl.value;
    const bgOpacityChanged = backgroundOpacityValue.value !== backgroundOpacity.value;
    if (bgUrlChanged || bgOpacityChanged) {
      await setBackground(backgroundUrl.value, backgroundOpacityValue.value);
      originalBackgroundUrl.value = backgroundUrl.value;
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
  backgroundOpacityValue.value = backgroundOpacity.value;
};
</script>
