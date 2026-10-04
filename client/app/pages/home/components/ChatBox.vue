<template>
  <!-- Floating layer anchor: the wrapper is relatively positioned and hosts the "scroll to bottom"
       floating button (absolutely positioned at the bottom center of the chat list).
       [overflow-hidden]: every overlay anchored here (progress float, turn scrubber, scroll
       button) is wider than a narrow chat column once both sidebars are open — the plan float's
       expanded panel is 320px against a ~280px column — and an unclipped overlay spills over the
       session list. Clipping at the column edge hides the overflow instead. -->
  <div class="relative flex flex-col flex-1 min-h-0 overflow-hidden">
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
            <!-- Background-task completion carrier / system-message card (USER row whose backend
                 origin is non-user, e.g. "subagent_completion"): rendered as a centered, muted
                 system card OUTSIDE the user bubble flow — the carrier announces a background
                 subagent completion, it is not something the user said. The first line
                 "[subagent:<name> <status>]" is self-describing and shown verbatim (no parsing).
                 USER rows always form singleton turn groups (see turnGroups), so a group holding
                 a carrier holds nothing else and the two loops below never interleave.
                 Collapsible and COLLAPSED BY DEFAULT: the header names the source, the body (the
                 full announcement) appears on click, matching the thinking/tool-card idiom. -->
            <div
              v-for="carrier in backgroundCarriers(rowGroup(vRow.index))"
              :key="carrier.id"
              class="background-task-card mx-auto flex w-full max-w-2xl flex-col items-center gap-1.5 rounded-lg border border-dashed border-gray-200 bg-gray-50/60 px-4 py-3 text-center dark:border-gray-700 dark:bg-gray-800/30">
              <button
                type="button"
                class="flex w-full cursor-pointer select-none items-center justify-center gap-1.5 text-xs font-medium tracking-wide text-[#9CA3AF] dark:text-[#6B7280]"
                :aria-expanded="expandedCarriers.has(carrier.id)"
                @click="toggleCarrier(carrier.id)">
                <span
                  aria-hidden="true"
                  class="pi pi-server text-[10px]"></span>
                {{ originLabel(carrier) }}
                <span
                  :class="[
                    'pi pi-chevron-down text-xs transition-transform duration-200',
                    { 'rotate-180': expandedCarriers.has(carrier.id) }
                  ]"></span>
              </button>
              <!-- Carrier body: verbatim plain text ({{ }} interpolation, no markdown round-trip);
                   whitespace preserved so the self-describing first line keeps its own line -->
              <div
                v-if="expandedCarriers.has(carrier.id)"
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
                  :highlighted="focusedMessageId === message.id"
                  :args-label="t('chatBox.toolArgs')"
                  :result-label="t('chatBox.toolResult')"
                  :running-label="t('chatBox.toolRunning')"
                  :no-output-label="t('chatBox.toolNoOutput')"
                  @toggle="toggleToolCard(message.id)" />
                <!-- Conversation content bubble: skipped when the message has nothing to
                     put in it — an AI turn that only produced reasoning keeps its thinking
                     block and metadata, but must not leave an empty white box behind. -->
                <div
                  v-else-if="hasBubbleBody(message)"
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
                  <ChatFileChangesChip
                    v-if="showRevertChip(message)"
                    class="mt-2"
                    :file-count="fileChanges.fileCount"
                    :can-revert="fileChanges.canRevert"
                    :revert-label="t('chatBox.revert')"
                    :unrevertable-label="t('chatBox.unrevertable')"
                    @open="openRevertDialog" />
                  <!-- A streaming row renders only its TAIL while it grows: one
                       multi-thousand-line row being re-laid-out on every flush starved
                       the page (menus, timers and fetches froze for tens of seconds on
                       a 2000-line answer). The settled row renders the full text. -->
                  <div
                    v-if="bubbleView(message).truncated"
                    class="mb-1 text-xs text-[#9CA3AF] dark:text-[#6B7280]">
                    {{ t('chatBox.streamingTail') }}
                  </div>
                  <!-- The v-safe-html directive handles markdown rendering + DOMPurify allowlist
                   sanitization internally (app/directives/safeHtml.ts) -->
                  <div v-safe-html="bubbleView(message).text"></div>
                  <!-- Settled long answer: the body above is a preview; this toggles
                       the full text (see bubbleView for the measured starvation). -->
                  <button
                    v-if="bubbleView(message).collapsed || expandedLongMessages.has(message.id)"
                    type="button"
                    class="mt-1 cursor-pointer select-none text-xs text-theme-main hover:underline"
                    @click="toggleLongMessage(message.id)">
                    {{
                      bubbleView(message).collapsed
                        ? t('chatBox.expandFull', { lines: bubbleView(message).lines })
                        : t('chatBox.collapseFull')
                    }}
                  </button>
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

    <!-- Turn scrubber: jump straight to any of the most recent user messages.
         Absolutely positioned, so it never takes part in the list's layout. -->
    <ChatTurnScrubber
      :marks="turnMarks"
      :active-index="activeMarkIndex"
      @jump="scrollToRow" />

    <!-- Plan progress read-out: top-right overlay, collapsed to a pill by default -->
    <ProgressFloat />

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

    <!-- Two-step revert: preview a plan, then confirm the destructive step. -->
    <ChatFileRevertDialog
      :visible="revertDialogOpen"
      :plan="revertPlan"
      :applying="revertApplying"
      @close="closeRevertDialog"
      @confirm="confirmRevert" />
  </div>
</template>

<script setup lang="ts">
// Methods/types
import type { MessageItem } from '../type';
import { CHAT_ROLE } from '../type';
import { formatCompactTimeString } from '@/common/utils';
import { useFileChangesStore } from '@/stores/file-changes';
import { useI18n } from 'vue-i18n';
// Render subcomponents (explicit imports: bare Vitest mounts have no Nuxt
// component auto-registration, and the page-level convention is explicit
// component imports)
import ChatMessageAvatar from '@/components/chat/ChatMessageAvatar.vue';
import ChatThinkingBlock from '@/components/chat/ChatThinkingBlock.vue';
import ChatToolCard from '@/components/chat/ChatToolCard.vue';
import ChatCopyButton from '@/components/chat/ChatCopyButton.vue';
import ChatFileChangesChip from '@/components/chat/ChatFileChangesChip.vue';
import ChatFileRevertDialog from '@/components/chat/ChatFileRevertDialog.vue';
import ChatMediaAttachments from '@/components/chat/ChatMediaAttachments.vue';
import ChatModelMeta from '@/components/chat/ChatModelMeta.vue';
import ChatTurnScrubber from './ChatTurnScrubber.vue';
import ProgressFloat from './ProgressFloat.vue';

const { t } = useI18n();

// File-change revert: the chip rides on the newest AI bubble of the turn, and
// the dialog previews the plan before anything is touched.
const fileChanges = useFileChangesStore();
// One listener registration per store instance; the refresh on `ws:connected`
// is what recovers the chip after a reload or a reconnect.
fileChanges.subscribe();
const revertDialogOpen = ref(false);
const revertPlan = ref<Awaited<ReturnType<typeof fileChanges.revert>> | null>(null);
const revertApplying = ref(false);

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

/**
 * The newest message the chat actually renders (the same accessor the template
 * uses, so a filtered-out carrier can never be mistaken for the last bubble).
 */
const lastVisibleMessage = computed<MessageItem | undefined>(() => {
  const groups = turnGroups.value;
  for (let i = groups.length - 1; i >= 0; i--) {
    const rows = regularMessages(groups[i] as never) as MessageItem[];
    if (rows.length) return rows[rows.length - 1];
  }
  return undefined;
});

/**
 * Whether the revert chip belongs on this message: the newest AI bubble of the
 * rendered conversation (one chip per session, not one per bubble).
 * @param message
 */
const showRevertChip = (message: MessageItem): boolean =>
  message.role === CHAT_ROLE.AI && lastVisibleMessage.value === message;

/** Open the dialog and load the read-only plan. */
const openRevertDialog = async (): Promise<void> => {
  revertDialogOpen.value = true;
  revertPlan.value = null;
  revertPlan.value = await fileChanges.revert({ dryRun: true });
};

const closeRevertDialog = (): void => {
  revertDialogOpen.value = false;
  revertPlan.value = null;
};

/** The destructive step: the plan showed exactly what this will do. */
const confirmRevert = async (): Promise<void> => {
  revertApplying.value = true;
  try {
    await fileChanges.revert({});
    closeRevertDialog();
  } finally {
    revertApplying.value = false;
  }
};

/** User display name: falls back to the i18n default when the prop is empty */
const resolvedUserName = computed(() => props.userName || t('chatBox.defaultUserName'));
/** AI display name: falls back to the i18n default when the prop is empty */
const resolvedAiName = computed(() => props.aiName || t('chatBox.defaultAiName'));

/**
 * Estimated token count of a user bubble's typed content (0 hides the line).
 * @param message
 */
const userTokenEstimate = (message: MessageItem): number => estimateTextTokens(message.content);

const emit = defineEmits<{
  (e: 'reach-top'): void;
  (e: 'release-head', messageIds: number[]): void;
}>();

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
  turnMarks,
  activeMarkIndex,
  scrollToBottom,
  scrollToRow,
  onScroll
} = useChatVirtualList(
  () => turnGroups.value,
  () => props.messages,
  {
    onReachTop: () => emit('reach-top'),
    // Memory cap: the oldest out-of-view rows are handed to the page, which drops
    // them from the in-memory history (paging refills them if the reader returns).
    onReleaseHead: messageIds => emit('release-head', messageIds)
  }
);
const { failedImageSources, onImageError } = useChatMedia();
const { copiedMessageId, canCopyMessage, copyMessage } = useMessageCopy();
const {
  expandedToolCards,
  expandedThinking,
  expandedCarriers,
  expandedLongMessages,
  toggleToolCard,
  toggleThinking,
  toggleCarrier,
  toggleLongMessage
} = useChatCardExpansion();

/**
 * Header label of a neutral (injector-origin) card, by the row's `origin`.
 *
 * Injected rows are not the user's words: the backend tags each producer with
 * an origin (`subagent_completion`, `task_intent`, `quality_gate`,
 * `todo_continuation`, ...) and the card says which one it was. An unknown
 * non-user origin still renders neutrally, under the generic label.
 */
const ORIGIN_LABEL_KEYS: Record<string, string> = {
  subagent_completion: 'chat.backgroundMessage',
  task_intent: 'chat.originTaskIntent',
  quality_gate: 'chat.originQualityGate',
  todo_continuation: 'chat.originTodoNudge'
};

/**
 * Header text of a neutral card.
 * @param message Injected USER row (non-user origin).
 */
const originLabel = (message: MessageItem): string => t(ORIGIN_LABEL_KEYS[message.origin ?? ''] ?? 'chat.originSystem');

//: What a STREAMING bubble renders at most: the tail's characters and lines.
const STREAM_TAIL_CHARS = 4000;
const STREAM_TAIL_LINES = 200;
//: A settled answer past this size renders a head preview plus an expand control.
const LONG_MESSAGE_CHARS = 4000;
const LONG_MESSAGE_PREVIEW_LINES = 20;

/** How a bubble renders one message: the text, and which control it needs. */
interface BubbleView {
  /** The markdown source this render shows. */
  text: string;
  /** Streaming tail-only preview (shows the 流式 hint line). */
  truncated: boolean;
  /** Settled long answer shown collapsed (shows the 展开全文 button). */
  collapsed: boolean;
  /** Total line count, for the expand label. */
  lines: number;
}

/**
 * The text a bubble renders for one message.
 *
 * Streaming rows render only their TAIL once the answer grows past the cap: the
 * row is re-laid-out on every flush, and one enormous row starves the page
 * (measured on a 2000-line answer: menus, timers and fetches all froze for tens
 * of seconds until the page caught up). Settled rows render a head PREVIEW and
 * an 展开全文 control instead — the starvation comes from having multi-thousand-
 * line rows at all, and the window usually holds several of them, so the cap has
 * to apply after the turn ends too (the same collapse idiom the tool cards and
 * thinking blocks already use). Nothing is lost either way: `message.content`
 * keeps the full text (copy button, tooltips, history all read it).
 * @param message
 */
const bubbleView = (message: MessageItem): BubbleView => {
  const content = message.content;
  const totalLines = content.split('\n').length;
  if (message.streaming) {
    if (content.length <= STREAM_TAIL_CHARS) {
      return { text: content, truncated: false, collapsed: false, lines: totalLines };
    }
    let tail = content.slice(-STREAM_TAIL_CHARS);
    // Start at a line boundary so the markdown structure of the tail is intact.
    const firstBreak = tail.indexOf('\n');
    if (firstBreak >= 0) tail = tail.slice(firstBreak + 1);
    const lines = tail.split('\n');
    if (lines.length > STREAM_TAIL_LINES) tail = lines.slice(-STREAM_TAIL_LINES).join('\n');
    return { text: tail, truncated: true, collapsed: false, lines: totalLines };
  }
  if (content.length > LONG_MESSAGE_CHARS && !expandedLongMessages.has(message.id)) {
    const preview = content.split('\n').slice(0, LONG_MESSAGE_PREVIEW_LINES).join('\n');
    return { text: preview, truncated: false, collapsed: true, lines: totalLines };
  }
  return { text: content, truncated: false, collapsed: false, lines: totalLines };
};

/**
 * Whether the bubble has anything to draw: text, or media attachments (a user
 * message can be attachment-only). Reasoning lives in its own block, so a
 * reasoning-only turn renders no bubble at all.
 * @param message
 */
const hasBubbleBody = (message: MessageItem): boolean =>
  message.content.trim().length > 0 ||
  messageImages(message).length > 0 ||
  messageAudios(message).length > 0 ||
  messageVideos(message).length > 0;

/**
 * Whether this message is a tool call card (tool cards are always expandable; live args/progress can be viewed even while running)
 * @param message
 */
const isToolMessage = (message: MessageItem): boolean => {
  return message.role === CHAT_ROLE.TOOL && !!message.toolName;
};

/** Message id briefly ringed after a jump into the list (null when idle). */
const focusedMessageId = ref<number | null>(null);
let focusTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * Bring one message into view (the toolbar's running-command entry jumps here:
 * a command's terminal output lives in its tool card, so the card is expanded
 * and briefly ringed instead of opening another panel).
 *
 * A message that is not in the loaded window is left alone — the caller only
 * ever passes an id of a row it just read from this session's message list.
 * @param messageId Client message id of the TOOL row.
 */
const scrollToMessage = (messageId: number): void => {
  const index = rows.value.findIndex(row => row.group.some(message => message.id === messageId));
  if (index < 0) return;
  expandedToolCards.add(messageId);
  scrollToRow(index);
  focusedMessageId.value = messageId;
  clearTimeout(focusTimer);
  focusTimer = setTimeout(() => {
    focusedMessageId.value = null;
  }, 2400);
};

onBeforeUnmount(() => clearTimeout(focusTimer));

defineExpose({ scrollToMessage });
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
      "revert": "撤销改动",
      "unrevertable": "改动不可撤销",
      "copy": "复制",
      "copied": "已复制",
      "defaultUserName": "我",
      "imageLoadFailed": "图片加载失败",
      "modelMeta": "输入 {input} · 输出 {output} tokens",
      "userInputMeta": "≈ {n} tokens",
      "scrollBottom": "回到最底部",
      "loadingOlder": "正在加载更早的消息",
      "streamingTail": "正在流式输出，仅显示末尾内容；本轮结束后显示全文",
      "expandFull": "展开全文（{lines} 行）",
      "collapseFull": "收起全文",
      "thinking": "思考过程",
      "toolArgs": "调用参数",
      "toolNoOutput": "无输出",
      "toolResult": "执行结果",
      "toolRunning": "执行中…"
    }
  },
  "en": {
    "chatBox": {
      "revert": "Undo changes",
      "unrevertable": "Changes not revertable",
      "copy": "Copy",
      "copied": "Copied",
      "defaultUserName": "Me",
      "imageLoadFailed": "Image load failed",
      "modelMeta": "{input} in · {output} out tokens",
      "userInputMeta": "≈ {n} tokens",
      "scrollBottom": "Scroll to bottom",
      "loadingOlder": "Loading earlier messages",
      "streamingTail": "Streaming — showing the end only; the full text appears when the turn ends",
      "expandFull": "Show all {lines} lines",
      "collapseFull": "Collapse",
      "thinking": "Thinking",
      "toolArgs": "Arguments",
      "toolNoOutput": "No output",
      "toolResult": "Result",
      "toolRunning": "Running…"
    }
  },
  "ja": {
    "chatBox": {
      "revert": "変更を元に戻す",
      "unrevertable": "変更は元に戻せません",
      "copy": "コピー",
      "copied": "コピーしました",
      "defaultUserName": "わたし",
      "imageLoadFailed": "画像の読み込みに失敗しました",
      "modelMeta": "入力 {input} · 出力 {output} tokens",
      "userInputMeta": "≈ {n} トークン",
      "scrollBottom": "最下部へ戻る",
      "loadingOlder": "以前のメッセージを読み込み中",
      "streamingTail": "ストリーミング中 — 末尾のみ表示しています。ターン終了後に全文を表示します",
      "expandFull": "全文を表示（{lines} 行）",
      "collapseFull": "折りたたむ",
      "thinking": "思考",
      "toolArgs": "引数",
      "toolNoOutput": "出力なし",
      "toolResult": "実行結果",
      "toolRunning": "実行中…"
    }
  },
  "ko": {
    "chatBox": {
      "revert": "변경 취소",
      "unrevertable": "변경을 되돌릴 수 없음",
      "copy": "복사",
      "copied": "복사됨",
      "defaultUserName": "나",
      "imageLoadFailed": "이미지 로드 실패",
      "modelMeta": "입력 {input} · 출력 {output} tokens",
      "userInputMeta": "≈ {n} 토큰",
      "scrollBottom": "맨 아래로",
      "loadingOlder": "이전 메시지 불러오는 중",
      "streamingTail": "스트리밍 중 — 끝부분만 표시하며, 턴이 끝나면 전체 내용이 표시됩니다",
      "expandFull": "전체 보기({lines}줄)",
      "collapseFull": "접기",
      "thinking": "생각",
      "toolArgs": "인자",
      "toolNoOutput": "출력 없음",
      "toolResult": "실행 결과",
      "toolRunning": "실행 중…"
    }
  }
}
</i18n>
