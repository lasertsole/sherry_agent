<template>
  <!-- First-run onboarding: welcome → pick a preset → core controls → start.
       Not dismissable by mask/Esc: the last step (or 跳过) is what records the
       persistent `onboarding_completed` flag, so a stray click cannot silently
       drop the user into an empty UI. -->
  <Dialog
    :visible="visible"
    modal
    :closable="false"
    :dismissable-mask="false"
    :show-header="false"
    :style="{ width: '560px' }"
    data-test="onboarding-dialog"
    @update:visible="value => emit('update:visible', value)">
    <div class="flex flex-col gap-4 p-2">
      <!-- Step 1: welcome -->
      <div
        v-if="step === 0"
        class="flex flex-col gap-2 text-center">
        <i
          class="pi pi-sparkles text-3xl text-theme-main"
          aria-hidden="true"></i>
        <h2 class="m-0 text-lg font-semibold text-gray-800 dark:text-gray-100">
          {{ t('onboarding.welcomeTitle') }}
        </h2>
        <p class="m-0 text-sm text-gray-500 dark:text-gray-400">{{ t('onboarding.welcomeDesc') }}</p>
      </div>

      <!-- Step 2: preset choice (built-ins only; the user has no presets yet) -->
      <div
        v-else-if="step === 1"
        class="flex flex-col gap-2">
        <h2 class="m-0 text-base font-semibold text-gray-800 dark:text-gray-100">
          {{ t('onboarding.choosePreset') }}
        </h2>
        <div class="grid grid-cols-2 gap-2">
          <button
            v-for="entry in entries"
            :key="entry.id"
            type="button"
            class="flex cursor-pointer flex-col gap-1 rounded-lg border border-solid p-3 text-left transition-colors"
            :class="
              selectedId === entry.id
                ? 'border-theme-main bg-theme-main/5'
                : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800'
            "
            :data-test="`onboarding-preset-${entry.id}`"
            @click="selectedId = entry.id">
            <span class="text-sm font-medium text-gray-700 dark:text-gray-200">{{ entryName(entry, t) }}</span>
            <span
              v-if="hasPresetDesc(entry.id)"
              class="text-xs leading-snug text-gray-500 dark:text-gray-400">
              {{ t(`onboarding.presetDesc.${entry.id}`) }}
            </span>
          </button>
        </div>
      </div>

      <!-- Step 3: the controls a first-time user needs to know -->
      <div
        v-else-if="step === 2"
        class="flex flex-col gap-2">
        <h2 class="m-0 text-base font-semibold text-gray-800 dark:text-gray-100">
          {{ t('onboarding.guideTitle') }}
        </h2>
        <div class="flex flex-col gap-3">
          <div
            v-for="guide in GUIDES"
            :key="guide.titleKey"
            class="flex items-start gap-3">
            <i
              :class="guide.icon"
              class="pi mt-0.5 text-lg text-theme-main"
              aria-hidden="true"></i>
            <div class="flex min-w-0 flex-col">
              <span class="text-sm font-medium text-gray-700 dark:text-gray-200">{{ t(guide.titleKey) }}</span>
              <span class="text-xs text-gray-500 dark:text-gray-400">{{ t(guide.descKey) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Step 4: ready -->
      <div
        v-else
        class="flex flex-col items-center gap-2 py-4 text-center">
        <i
          class="pi pi-check-circle text-3xl text-theme-main"
          aria-hidden="true"></i>
        <p class="m-0 text-sm text-gray-700 dark:text-gray-200">{{ t('onboarding.ready') }}</p>
      </div>

      <p
        v-if="error"
        class="m-0 text-xs text-red-500"
        data-test="onboarding-error">
        {{ error }}
      </p>

      <!-- Navigation -->
      <div class="flex items-center justify-between gap-2">
        <Button
          v-if="step > 0"
          :label="t('onboarding.prev')"
          size="small"
          text
          :disabled="creating"
          data-test="onboarding-prev"
          @click="step -= 1" />
        <span v-else />
        <div class="flex items-center gap-2">
          <Button
            :label="t('onboarding.skip')"
            size="small"
            text
            severity="secondary"
            :disabled="creating"
            data-test="onboarding-skip"
            @click="skip" />
          <Button
            v-if="step < 3"
            :label="t('onboarding.next')"
            size="small"
            data-test="onboarding-next"
            @click="step += 1" />
          <Button
            v-else
            :label="t('onboarding.start')"
            size="small"
            :loading="creating"
            data-test="onboarding-start"
            @click="finish" />
        </div>
      </div>
    </div>
  </Dialog>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';

import type { PresetEntry } from '~/composables/persona-catalog';
// The page owns the character snapshot helper; injected into the shared
// creator so the catalogue module never imports a component.
import { ensureSessionCharacter } from '~/pages/home/components/SessionSidebar.vue';
import { logUtil } from '~/utils/log';

/**
 * First-run onboarding (4 steps). The LAST step (or 跳过) creates the session
 * from the chosen preset and records the persistent flag, so the flow never
 * reappears on a later visit.
 *
 * The flag is written through `useAppFlags` (Dexie `appFlags` + a localStorage
 * mirror): a browser whose IndexedDB is blocked still remembers that the tour
 * was completed instead of showing it on every entry.
 */
const props = defineProps<{ visible: boolean }>();

const emit = defineEmits<{
  'update:visible': [boolean];
  /** The user skipped: the shell opens the ordinary preset dialog instead. */
  skip: [];
}>();

const { t, locale, te } = useI18n();
const router = useRouter();
const localePath = useLocalePath();
const agentStore = useAgentConfigStore();

/** Steps: 0 welcome · 1 preset · 2 guides · 3 ready. */
const step = ref(0);
/** Chosen catalogue id (the default preset until the user picks another). */
const selectedId = ref<string>(DEFAULT_PRESET_ID);
const creating = ref(false);
const error = ref('');

/** Built-in presets only: a first-run user has no saved presets yet. */
const entries = computed<PresetEntry[]>(() => catalogEntries([]));

/**
 * Whether the locale carries a description for this preset id.
 * @param id
 */
const hasPresetDesc = (id: string): boolean => te(`onboarding.presetDesc.${id}`);

/** The three controls the tour points at. */
const GUIDES = [
  { icon: 'pi-send', titleKey: 'onboarding.guideSend', descKey: 'onboarding.guideSendDesc' },
  { icon: 'pi-bolt', titleKey: 'onboarding.guideToolbar', descKey: 'onboarding.guideToolbarDesc' },
  { icon: 'pi-list', titleKey: 'onboarding.guideFold', descKey: 'onboarding.guideFoldDesc' }
] as const;

// Every open starts at step 1 with the default preset.
watch(
  () => props.visible,
  open => {
    if (!open) return;
    step.value = 0;
    selectedId.value = DEFAULT_PRESET_ID;
    creating.value = false;
    error.value = '';
  },
  { immediate: true }
);

/** Record the flag and close (shared by skip and finish). */
const complete = async (): Promise<void> => {
  await setAppFlag('onboarding_completed', true);
  emit('update:visible', false);
};

/** Skip the tour: remember it, then hand the user the ordinary preset dialog. */
const skip = async (): Promise<void> => {
  await complete();
  emit('skip');
};

/** Finish: create the session from the chosen preset, then remember the tour. */
const finish = async (): Promise<void> => {
  if (creating.value) return;
  const entry = entries.value.find(candidate => candidate.id === selectedId.value);
  if (!entry) return;
  creating.value = true;
  error.value = '';
  try {
    // The tool catalogue comes first: Pure / Companion derive their agent block
    // from it (a preset applied without it would write the wrong set).
    await agentStore.loadCatalog();
    await createSessionFromPreset(entry, {
      locale: locale.value,
      t,
      facts: presetCatalogFacts(agentStore),
      router,
      localePath,
      ensureCharacter: ensureSessionCharacter
    });
    await complete();
  } catch (e) {
    logUtil.e('[OnboardingDialog] Failed to create the session:', e);
    const detail = e instanceof Error ? e.message : String(e);
    error.value = detail ? `${t('onboarding.createFailed')} (${detail})` : t('onboarding.createFailed');
  } finally {
    creating.value = false;
  }
};
</script>

<i18n lang="json">
{
  "zh": {
    "onboarding": {
      "welcomeTitle": "欢迎使用 Sherry",
      "welcomeDesc": "一个带长期记忆与任务编排的 AI 助手。用一分钟完成初始设置。",
      "choosePreset": "选择一个预设开始",
      "presetDesc": {
        "pure": "无预设配置，最小化工具面",
        "companion": "角色扮演，关闭任务编排",
        "sherry": "全部能力开启"
      },
      "guideTitle": "核心操作",
      "guideSend": "发送消息",
      "guideSendDesc": "输入框输入后按 Enter 发送；运行中继续输入会排队。",
      "guideToolbar": "工具栏",
      "guideToolbarDesc": "输入框上方可切换多媒体、访问模式、模型、思考等级与项目目录。",
      "guideFold": "Turn 折叠",
      "guideFoldDesc": "执行过程默认折叠，点击摘要可展开工具调用与思考。",
      "ready": "一切就绪，开始对话吧",
      "prev": "上一步",
      "next": "下一步",
      "skip": "跳过",
      "start": "开始使用",
      "createFailed": "创建会话失败"
    }
  },
  "en": {
    "onboarding": {
      "welcomeTitle": "Welcome to Sherry",
      "welcomeDesc": "An AI assistant with long-term memory and task orchestration. One minute of setup.",
      "choosePreset": "Pick a preset to start",
      "presetDesc": {
        "pure": "No preset configuration, minimal tool surface",
        "companion": "Role-play; task orchestration off",
        "sherry": "Everything on"
      },
      "guideTitle": "The essentials",
      "guideSend": "Send a message",
      "guideSendDesc": "Type and press Enter; typing during a running turn queues the message.",
      "guideToolbar": "Toolbar",
      "guideToolbarDesc": "Above the input: media, access mode, model, thinking level and project directory.",
      "guideFold": "Turn folding",
      "guideFoldDesc": "Execution is folded by default; click the summary to expand tool calls and thinking.",
      "ready": "All set — start chatting",
      "prev": "Back",
      "next": "Next",
      "skip": "Skip",
      "start": "Start",
      "createFailed": "Could not create the session"
    }
  },
  "ja": {
    "onboarding": {
      "welcomeTitle": "Sherry へようこそ",
      "welcomeDesc": "長期記憶とタスク編成を備えた AI アシスタントです。1 分で初期設定を終えましょう。",
      "choosePreset": "プリセットを選んで開始",
      "presetDesc": {
        "pure": "プリセット設定なし、最小限のツール",
        "companion": "ロールプレイ、タスク編成はオフ",
        "sherry": "すべての機能を有効"
      },
      "guideTitle": "基本操作",
      "guideSend": "メッセージ送信",
      "guideSendDesc": "入力して Enter で送信。実行中に入力するとキューに入ります。",
      "guideToolbar": "ツールバー",
      "guideToolbarDesc": "入力欄の上でメディア、アクセス モード、モデル、思考レベル、プロジェクト ディレクトリを切り替えます。",
      "guideFold": "ターンの折りたたみ",
      "guideFoldDesc": "実行過程は既定で折りたたまれます。概要をクリックするとツール呼び出しと思考が開きます。",
      "ready": "準備完了。会話を始めましょう",
      "prev": "戻る",
      "next": "次へ",
      "skip": "スキップ",
      "start": "開始",
      "createFailed": "セッションを作成できませんでした"
    }
  },
  "ko": {
    "onboarding": {
      "welcomeTitle": "Sherry에 오신 것을 환영합니다",
      "welcomeDesc": "장기 기억과 작업 오케스트레이션을 갖춘 AI 비서입니다. 1분이면 초기 설정을 끝낼 수 있습니다.",
      "choosePreset": "시작할 프리셋 선택",
      "presetDesc": {
        "pure": "프리셋 없음, 최소 도구 구성",
        "companion": "역할극, 작업 오케스트레이션 끔",
        "sherry": "모든 기능 켜짐"
      },
      "guideTitle": "핵심 조작",
      "guideSend": "메시지 보내기",
      "guideSendDesc": "입력 후 Enter로 전송하며, 실행 중에 입력하면 대기열에 쌓입니다.",
      "guideToolbar": "툴바",
      "guideToolbarDesc": "입력창 위에서 미디어, 접근 모드, 모델, 사고 수준, 프로젝트 디렉터리를 바꿉니다.",
      "guideFold": "턴 접기",
      "guideFoldDesc": "실행 과정은 기본으로 접혀 있으며 요약을 누르면 도구 호출과 사고가 펼쳐집니다.",
      "ready": "준비 완료 — 대화를 시작하세요",
      "prev": "이전",
      "next": "다음",
      "skip": "건너뛰기",
      "start": "시작",
      "createFailed": "세션을 만들지 못했습니다"
    }
  }
}
</i18n>
