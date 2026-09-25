<template>
  <!-- Floating layer anchor: the wrapper is relatively positioned and hosts the "scroll to bottom"
       floating button (absolutely positioned at the bottom center of the chat list) -->
  <div class="relative flex flex-col flex-1 min-h-0">
    <!-- [scrollbar-gutter:stable]: always reserves a gutter for the (classic) scrollbar,
         preventing content from shifting horizontally when switching between the no-scrollbar and
         scrollbar states (e.g. empty state "start a new conversation" → messages accumulate) -->
    <div
      ref="scrollContainerRef"
      class="flex-1 min-h-0 border-b border-solid border-gray-light dark:border-gray-dark overflow-auto px-6 py-4 [scrollbar-gutter:stable]"
      @scroll="onScroll">
      <!-- Virtualized window: positioned rows inside a spacer of the measured
           total height. `measureElement` re-measures on resize, so expandable
           tool cards / thinking blocks keep the scroll geometry correct. -->
      <div
        class="relative w-full"
        :style="{ height: `${totalSize}px` }">
        <div
          v-for="vRow in virtualRows"
          :key="String(vRow.key)"
          :ref="el => virtualizer.measureElement(el as HTMLElement)"
          :data-index="vRow.index"
          :class="['absolute left-0 top-0 w-full', vRow.index < rows.length - 1 ? 'pb-6' : '']"
          :style="{ transform: `translateY(${vRow.start}px)` }">
          <div
            v-if="rowGroup(vRow.index).length"
            :class="['flex flex-col min-w-0', { 'gap-3': turnSpacingClass(rowGroup(vRow.index)) }]">
            <!-- Background-task completion carrier (USER row whose backend origin="subagent_completion"):
                 rendered as a centered, muted system card OUTSIDE the user bubble flow — the carrier
                 announces a background subagent completion, it is not something the user said. The
                 first line "[subagent:<name> <status>]" is self-describing and shown verbatim (no
                 parsing). USER rows always form singleton turn groups (see turnGroups), so a group
                 holding a carrier holds nothing else and the two loops below never interleave. -->
            <div
              v-for="carrier in backgroundCarriers(rowGroup(vRow.index))"
              :key="carrier.id"
              class="background-task-card mx-auto flex w-full max-w-2xl flex-col items-center gap-1.5 rounded-lg border border-dashed border-gray-200 bg-gray-50/60 px-4 py-3 text-center dark:border-gray-700 dark:bg-gray-800/30">
              <span
                class="flex items-center gap-1.5 text-xs font-medium tracking-wide text-[#9CA3AF] dark:text-[#6B7280]">
                <span
                  aria-hidden="true"
                  class="pi pi-server text-[10px]"></span>
                {{ t('chat.backgroundMessage') }}
              </span>
              <!-- Carrier body: verbatim plain text ({{ }} interpolation, no markdown round-trip);
                   whitespace preserved so the self-describing first line keeps its own line -->
              <div
                class="w-full whitespace-pre-wrap break-words text-left text-sm leading-relaxed text-gray-500 dark:text-gray-400">
                {{ carrier.content }}
              </div>
            </div>
            <div
              v-for="message in regularMessages(rowGroup(vRow.index))"
              :key="message.id"
              :class="[
                'flex justify-start gap-3 min-w-0',
                { 'flex-row-reverse text-right': message.role === CHAT_ROLE.USER },
                { 'text-left': message.role === CHAT_ROLE.AI }
              ]">
              <ChatMessageAvatar
                :src="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
                :alt="message.role === CHAT_ROLE.USER ? resolvedUserName : resolvedAiName"
                :hidden="isConsecutive(message.id) || message.role === CHAT_ROLE.TOOL" />
              <!-- Message body -->
              <div
                :class="[
                  'flex flex-col max-w-[calc(100%_-_52px)] min-w-0',
                  message.role === CHAT_ROLE.USER ? 'items-end' : 'items-start'
                ]">
                <!-- User/AI timestamp -->
                <div
                  v-if="message.role !== CHAT_ROLE.TOOL"
                  :class="[
                    'flex items-center gap-2 mb-1',
                    { 'text-right justify-end': message.role === CHAT_ROLE.USER },
                    { 'text-left': message.role === CHAT_ROLE.AI }
                  ]">
                  <span class="text-sm font-semibold text-[#111827] dark:text-[#E5E7EB]">{{
                    message.role === CHAT_ROLE.AI ? resolvedAiName : resolvedUserName
                  }}</span>
                  <span class="text-xs font-normal text-[#6B7280] dark:text-[#9CA3AF]">{{
                    formatCompactTimeString(message.timestamp)
                  }}</span>
                </div>
                <!-- Model thinking/reasoning block (collapsible): rendered only for AI messages that contain reasoning -->
                <ChatThinkingBlock
                  v-if="message.role === CHAT_ROLE.AI && message.reasoning"
                  :reasoning="message.reasoning ?? ''"
                  :expanded="expandedThinking.has(message.id)"
                  :consecutive="isConsecutive(message.id)"
                  :label="t('chatBox.thinking')"
                  @toggle="toggleThinking(message.id)" />
                <!-- Tool call card -->
                <ChatToolCard
                  v-if="message.role === CHAT_ROLE.TOOL"
                  :message="message"
                  :expanded="expandedToolCards.has(message.id)"
                  :expandable="isToolMessage(message)"
                  :args-label="t('chatBox.toolArgs')"
                  :result-label="t('chatBox.toolResult')"
                  :running-label="t('chatBox.toolRunning')"
                  :no-output-label="t('chatBox.toolNoOutput')"
                  @toggle="toggleToolCard(message.id)" />
                <!-- Conversation content bubble -->
                <div
                  v-else
                  :class="[
                    'relative group w-fit p-3 text-sm font-normal leading-relaxed shadow-sm break-words transition-colors duration-200',
                    message.role === CHAT_ROLE.USER
                      ? 'bg-[#2563EB] text-[#FFFFFF] rounded-s-xl rounded-ee-xl dark:bg-[#3B82F6]' /* Right-side bubble: blue, with custom bottom-left/bottom-right corner radii */
                      : 'bg-white text-gray-900 rounded-e-xl rounded-es-xl border border-gray-100' /* Left-side bubble: white */,
                    { 'rounded-xl': isConsecutive(message.id) }
                  ]">
                  <ChatCopyButton
                    v-if="canCopyMessage(message)"
                    :copied="copiedMessageId === message.id"
                    :is-user="message.role === CHAT_ROLE.USER"
                    :copy-label="t('chatBox.copy')"
                    :copied-label="t('chatBox.copied')"
                    @copy="copyMessage(message)" />
                  <!-- The v-safe-html directive handles markdown rendering + DOMPurify allowlist
                   sanitization internally (app/directives/safeHtml.ts) -->
                  <div v-safe-html="message.content"></div>
                  <ChatMediaAttachments
                    :message="message"
                    :failed-sources="failedImageSources"
                    :preview-label="t('a11y.previewImage')"
                    :load-failed-label="t('chatBox.imageLoadFailed')"
                    :image-error-handler="onImageError" />
                </div>
                <!-- Content token estimate under the user bubble (local heuristic, ≈ marks it apart
                 from the AI bubble's exact provider accounting) -->
                <div
                  v-if="message.role === CHAT_ROLE.USER && userTokenEstimate(message) > 0"
                  class="mt-1 text-xs text-[#9CA3AF] dark:text-[#6B7280]">
                  {{ t('chatBox.userInputMeta', { n: userTokenEstimate(message) }) }}
                </div>
                <!-- Model metadata (model name + token usage; shown only for AI messages when the fields exist) -->
                <ChatModelMeta
                  v-if="
                    message.role === CHAT_ROLE.AI &&
                    (message.modelName || message.inputTokens !== undefined || message.outputTokens !== undefined)
                  "
                  :model-name="message.modelName"
                  :input-tokens="message.inputTokens"
                  :output-tokens="message.outputTokens"
                  :text="
                    t('chatBox.modelMeta', {
                      input: message.inputTokens ?? 0,
                      output: message.outputTokens ?? 0
                    })
                  " />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Older-history loading pill: overlays the top of the list while a scroll-up page request runs -->
    <Transition name="fade">
      <div
        v-if="loadingOlder"
        class="pointer-events-none absolute top-3 left-0 right-0 z-10 flex justify-center">
        <span
          class="flex items-center gap-2 rounded-full bg-black/40 px-3 py-1 text-xs text-white backdrop-blur-sm dark:bg-white/20">
          <i
            class="pi pi-spin pi-spinner text-[10px]"
            aria-hidden="true"></i>
          {{ t('chatBox.loadingOlder') }}
        </span>
      </div>
    </Transition>
    <!-- Scroll to bottom: floats at the bottom center of the chat list; appears only when the
         scroll position is more than 80px (NEAR_BOTTOM_THRESHOLD) from the bottom; translucent +
         frosted glass; clicking scrolls back to the very bottom (the subsequent scroll event
         auto-hides the button). Centering uses left-0/right-0 + mx-auto (not translate), avoiding
         conflicts with the Transition's transform animation. -->
    <Transition name="fade">
      <button
        v-if="showScrollBottom"
        type="button"
        class="absolute bottom-4 left-0 right-0 mx-auto z-10 flex justify-center items-center w-9 h-9 rounded-full border border-solid border-white/20 bg-black/30 text-white hover:bg-black/45 dark:bg-white/20 dark:border-white/10 dark:hover:bg-white/30 backdrop-blur-sm"
        :aria-label="t('chatBox.scrollBottom')"
        :title="t('chatBox.scrollBottom')"
        @click="scrollToBottom">
        <i class="pi pi-arrow-down text-sm"></i>
      </button>
    </Transition>
  </div>
</template>

<script setup lang="ts">
// Methods/types
import type { MessageItem } from '../type';
import { CHAT_ROLE } from '../type';
import { formatCompactTimeString } from '@/common/utils';
import { useI18n } from 'vue-i18n';
// Render subcomponents (explicit imports: bare Vitest mounts have no Nuxt
// component auto-registration, and the page-level convention is explicit
// component imports)
import ChatMessageAvatar from '@/components/chat/ChatMessageAvatar.vue';
import ChatThinkingBlock from '@/components/chat/ChatThinkingBlock.vue';
import ChatToolCard from '@/components/chat/ChatToolCard.vue';
import ChatCopyButton from '@/components/chat/ChatCopyButton.vue';
import ChatMediaAttachments from '@/components/chat/ChatMediaAttachments.vue';
import ChatModelMeta from '@/components/chat/ChatModelMeta.vue';

const { t } = useI18n();

interface Props {
  /** Scroll-up history request in flight (shows the top loading pill). */
  loadingOlder?: boolean;
  messages: MessageItem[] | undefined;
  /** User avatar URL (returned by the server) */
  userAvatar?: string;
  /** AI avatar URL (returned by the server) */
  aiAvatar?: string;
  /** User display name (returned by the server) */
  userName?: string;
  /** AI display name (returned by the server) */
  aiName?: string;
}
const props = withDefaults(defineProps<Props>(), {
  messages: () => [] as MessageItem[],
  userAvatar: '',
  aiAvatar: '',
  userName: '',
  aiName: '',
  loadingOlder: false
});

/** User display name: falls back to the i18n default when the prop is empty */
const resolvedUserName = computed(() => props.userName || t('chatBox.defaultUserName'));
/** AI display name: falls back to the i18n default when the prop is empty */
const resolvedAiName = computed(() => props.aiName || t('chatBox.defaultAiName'));

/**
 * Estimated token count of a user bubble's typed content (0 hides the line).
 * @param message
 */
const userTokenEstimate = (message: MessageItem): number => estimateTextTokens(message.content);

const emit = defineEmits<{ (e: 'reach-top'): void }>();

// View-model: turn grouping, scroll, media URL resolution, card expansion, copy
const { isConsecutive, turnGroups, turnSpacingClass, regularMessages, backgroundCarriers } = useChatTurnGroups(
  () => props.messages
);
const {
  scrollContainerRef,
  rows,
  virtualRows,
  virtualizer,
  totalSize,
  rowGroup,
  showScrollBottom,
  scrollToBottom,
  onScroll
} = useChatVirtualList(
  () => turnGroups.value,
  () => props.messages,
  { onReachTop: () => emit('reach-top') }
);
const { failedImageSources, onImageError } = useChatMedia();
const { copiedMessageId, canCopyMessage, copyMessage } = useMessageCopy();
const { expandedToolCards, expandedThinking, toggleToolCard, toggleThinking } = useChatCardExpansion();

/**
 * Whether this message is a tool call card (tool cards are always expandable; live args/progress can be viewed even while running)
 * @param message
 */
const isToolMessage = (message: MessageItem): boolean => {
  return message.role === CHAT_ROLE.TOOL && !!message.toolName;
};
</script>

<style scoped>
/* "Scroll to bottom" floating button: fade in/out + slight upward float (Vue Transition) */
.fade-enter-active,
.fade-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>

<i18n lang="json">
{
  "zh": {
    "chatBox": {
      "copy": "复制",
      "copied": "已复制",
      "defaultUserName": "我",
      "imageLoadFailed": "图片加载失败",
      "modelMeta": "输入 {input} · 输出 {output} tokens",
      "userInputMeta": "≈ {n} tokens",
      "scrollBottom": "回到最底部",
      "loadingOlder": "正在加载更早的消息",
      "thinking": "思考过程",
      "toolArgs": "调用参数",
      "toolNoOutput": "无输出",
      "toolResult": "执行结果",
      "toolRunning": "执行中…"
    }
  },
  "en": {
    "chatBox": {
      "copy": "Copy",
      "copied": "Copied",
      "defaultUserName": "Me",
      "imageLoadFailed": "Image load failed",
      "modelMeta": "{input} in · {output} out tokens",
      "userInputMeta": "≈ {n} tokens",
      "scrollBottom": "Scroll to bottom",
      "loadingOlder": "Loading earlier messages",
      "thinking": "Thinking",
      "toolArgs": "Arguments",
      "toolNoOutput": "No output",
      "toolResult": "Result",
      "toolRunning": "Running…"
    }
  },
  "ja": {
    "chatBox": {
      "copy": "コピー",
      "copied": "コピーしました",
      "defaultUserName": "わたし",
      "imageLoadFailed": "画像の読み込みに失敗しました",
      "modelMeta": "入力 {input} · 出力 {output} tokens",
      "userInputMeta": "≈ {n} トークン",
      "scrollBottom": "最下部へ戻る",
      "loadingOlder": "以前のメッセージを読み込み中",
      "thinking": "思考",
      "toolArgs": "引数",
      "toolNoOutput": "出力なし",
      "toolResult": "実行結果",
      "toolRunning": "実行中…"
    }
  },
  "ko": {
    "chatBox": {
      "copy": "복사",
      "copied": "복사됨",
      "defaultUserName": "나",
      "imageLoadFailed": "이미지 로드 실패",
      "modelMeta": "입력 {input} · 출력 {output} tokens",
      "userInputMeta": "≈ {n} 토큰",
      "scrollBottom": "맨 아래로",
      "loadingOlder": "이전 메시지 불러오는 중",
      "thinking": "생각",
      "toolArgs": "인자",
      "toolNoOutput": "출력 없음",
      "toolResult": "실행 결과",
      "toolRunning": "실행 중…"
    }
  }
}
</i18n>
