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
      @scroll="onScroll"
      @contextmenu="onMessageContextMenu">
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
            <!-- Injected-carrier / system-message card (a row whose backend origin is non-user,
                 e.g. "subagent_completion" and the injected notices "project_dir" / "git_head"): rendered
                 as a centered, muted system card OUTSIDE the bubble flow — it is neither
                 something the user said nor the assistant's reply. The completion carrier's
                 first line "[subagent:<name> <status>]" is self-describing and shown verbatim
                 (no parsing); the directory notice carries its full switch sentence.
                 Every carrier forms its OWN turn group (see turnGroups), so a group holding a
                 carrier holds nothing else and the two loops below never interleave — that is
                 what keeps the directory notice directly above the human message it explains
                 (it sorts immediately before that message) instead of drifting up into the
                 previous turn's answer.
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
                  :class="['text-[10px]', originIcon(carrier)]"></span>
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
            <!-- Settled turn with work behind it: the execution history folds
                 behind this summary row, while the final assistant answer stays
                 visible below. An ineligible group (running, interrupted, or a
                 lone answer) renders linearly exactly as before. -->
            <ChatTurnHistoryStatus
              v-if="groupFold(vRow.index).eligible"
              :label="foldLabel(groupFold(vRow.index))"
              :expanded="expandedTurnHistory.has(groupFold(vRow.index).key)"
              :expand-label="t('chatBox.turnHistoryExpand')"
              :collapse-label="t('chatBox.turnHistoryCollapse')"
              class="ml-[52px]"
              @toggle="toggleTurnHistory(groupFold(vRow.index).key)" />
            <div
              v-for="message in groupMessages(rowGroup(vRow.index))"
              :key="message.id"
              :data-message-id="String(message.id)"
              :data-message-role="message.role"
              :class="[
                'flex justify-start gap-3 min-w-0',
                { 'flex-row-reverse text-right': message.role === CHAT_ROLE.USER },
                { 'text-left': message.role === CHAT_ROLE.AI }
              ]">
              <ChatMessageAvatar
                :src="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
                :alt="message.role === CHAT_ROLE.USER ? resolvedUserName : resolvedAiName"
                :hidden="
                  (isConsecutive(message.id) && !isFoldedFinal(message, rowGroup(vRow.index))) ||
                  message.role === CHAT_ROLE.TOOL
                " />
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
                <!-- Live model-retry progress: the assistant row itself carries the
                     attempt counter, so the retry is part of the history queue rather
                     than a detached toast. Settled rows keep the final count. -->
                <div
                  v-if="message.role === CHAT_ROLE.AI && message.retryStatus"
                  class="mb-1 flex items-center gap-1.5 rounded-md border border-solid border-amber-200/80 bg-amber-50 px-2 py-1 text-[11px] text-amber-700 dark:border-amber-700/50 dark:bg-amber-950/30 dark:text-amber-300">
                  <i
                    :class="[
                      message.retryStatus.status === 'scheduled'
                        ? 'pi pi-spin pi-spinner'
                        : message.retryStatus.status === 'succeeded'
                          ? 'pi pi-replay'
                          : 'pi pi-exclamation-triangle'
                    ]"
                    class="text-[10px]"></i>
                  <span>{{ retryStatusLabel(message.retryStatus) }}</span>
                </div>
                <!-- Model thinking/reasoning block (collapsible): rendered only for AI messages that contain reasoning -->
                <ChatThinkingBlock
                  v-if="
                    message.role === CHAT_ROLE.AI && message.reasoning && !isFoldedFinal(message, rowGroup(vRow.index))
                  "
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
                  :trajectory-label="t('chatBox.openTrajectory')"
                  @toggle="toggleToolCard(message.id)"
                  @open-trajectory="openTrajectory" />
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
                    {
                      'rounded-xl': isConsecutive(message.id) && !isFoldedFinal(message, rowGroup(vRow.index))
                    }
                  ]">
                  <ChatCopyButton
                    v-if="canCopyMessage(message)"
                    :copied="copiedMessageId === message.id"
                    :is-user="message.role === CHAT_ROLE.USER"
                    :copy-label="t('chatBox.copy')"
                    :copied-label="t('chatBox.copied')"
                    @copy="copyMessage(message)" />
                  <!-- Rewrite a SENT user message (pencil floats beside the copy
                       button, fades in with the bubble hover). The backend
                       truncates that turn onward and re-runs the edited text.
                       Persisted user rows only: a streaming row has a temp id, and
                       an approval card owns the composer. -->
                  <button
                    v-if="canEditMessage(message)"
                    type="button"
                    class="absolute top-2 right-11 flex h-7 w-7 items-center justify-center rounded-full border border-solid border-white/20 bg-blue-700/40 text-blue-100 opacity-0 transition-opacity duration-200 hover:bg-blue-600/50 hover:text-white focus-visible:opacity-100 group-hover:opacity-100"
                    :title="t('chatBox.editMessage')"
                    :aria-label="t('chatBox.editMessage')"
                    data-test="chat-edit-message"
                    @click="emit('edit-message', message)">
                    <i class="pi pi-pencil text-xs"></i>
                  </button>
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
                   sanitization internally (app/directives/safeHtml.ts); the data-streaming
                   attribute tells its enhancement pass (Shiki/Mermaid/KaTeX) to wait until
                   the stream settles — a streaming row pays zero enhancement cost. -->
                  <div
                    v-safe-html="bubbleView(message).text"
                    :data-streaming="message.streaming ? 'true' : undefined"></div>
                  <!-- Settled long answer: the body above is a preview; this toggles
                       the full text (see bubbleView for the measured starvation). -->
                  <button
                    v-if="bubbleView(message).collapsed || expandedLongMessages.has(message.id)"
                    type="button"
                    class="mt-1 cursor-pointer select-none text-xs text-[#2563EB] hover:underline"
                    @click="toggleLongMessage(message.id)">
                    {{
                      bubbleView(message).collapsed
                        ? t('chatBox.expandFull', { chars: bubbleView(message).chars })
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
                  "
                  :title="t('chatBox.contextTip')"
                  @open-context="openContextViewer" />
                <!-- The answer was cut off (Stop / heartbeat timeout) but the
                     backend still holds its partial text: continue it instead of
                     re-sending the question. Only the LAST row qualifies. -->
                <button
                  v-if="continueTurn !== null && message.turn_num === continueTurn"
                  type="button"
                  class="mt-1 flex items-center gap-1 text-xs text-[#2563EB] hover:underline"
                  data-test="chat-continue-generation"
                  @click="emit('continue-generation', message.turn_num)">
                  <i class="pi pi-play text-[10px]"></i>
                  {{ t('chatBox.continueGeneration') }}
                </button>
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
    <!-- One menu for the whole list: a right-click on a valid text selection
         opens it anchored at the pointer. A right-click with no selection (or
         on a control) is left alone so the native copy menu still works. -->
    <ContextMenu
      ref="selectionMenuRef"
      :model="selectionMenuItems"
      data-test="chat-selection-menu" />
  </div>
</template>

<script setup lang="ts">
// Methods/types
import type { MessageItem } from '../type';
import { CHAT_ROLE } from '../type';
import { formatCompactTimeString, formatToolDuration } from '@/common/utils';
// Value is auto-imported by Nuxt; only the type needs an explicit import.
import type { TurnFoldView } from '@/composables/use-chat-turn-fold';
import type { SelectionPayload } from '@/composables/use-chat-selection';
import { useI18n } from 'vue-i18n';
// PrimeVue's ContextMenu is registered globally by the app plugin; an explicit
// import keeps the transcript mountable in bare Vitest mounts too.
import ContextMenu from 'primevue/contextmenu';
// Render subcomponents (explicit imports: bare Vitest mounts have no Nuxt
// component auto-registration, and the page-level convention is explicit
// component imports)
import ChatMessageAvatar from '@/components/chat/ChatMessageAvatar.vue';
import ChatThinkingBlock from '@/components/chat/ChatThinkingBlock.vue';
import ChatToolCard from '@/components/chat/ChatToolCard.vue';
import ChatCopyButton from '@/components/chat/ChatCopyButton.vue';
import ChatMediaAttachments from '@/components/chat/ChatMediaAttachments.vue';
import ChatModelMeta from '@/components/chat/ChatModelMeta.vue';
import ChatTurnHistoryStatus from '@/components/chat/ChatTurnHistoryStatus.vue';
import ChatTurnScrubber from './ChatTurnScrubber.vue';
import ProgressFloat from './ProgressFloat.vue';

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
  /** Whether a text selection opens the quote menu (false while an approval card owns the input). */
  selectionEnabled?: boolean;
  /** Whether the menu offers "ask in side session" (the side panel itself only re-quotes). */
  allowSideChat?: boolean;
  /** Whether a settled row may be edited (false while a turn streams). */
  canEdit?: boolean;
}
const props = withDefaults(defineProps<Props>(), {
  messages: () => [] as MessageItem[],
  userAvatar: '',
  aiAvatar: '',
  userName: '',
  aiName: '',
  loadingOlder: false,
  selectionEnabled: true,
  allowSideChat: true,
  canEdit: true
});

const rightSidebar = useRightSidebarStore();

/**
 * Open (or focus) the session's context viewer tab. The store dedupes by kind,
 * so clicking any number in any bubble lands in that one tab.
 */
const openContextViewer = (): void => {
  rightSidebar.openTab('contextViewer');
};

/**
 * Open (or focus) the session's trajectory panel. Like the context viewer it
 * is session-scoped, and the store dedupes by kind.
 */
const openTrajectory = (): void => {
  rightSidebar.openTab('trajectory');
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

/**
 * One-line label for the live/settled model-retry indicator.
 * @param retry The row's retry progress payload.
 */
const retryStatusLabel = (retry: NonNullable<MessageItem['retryStatus']>): string => {
  if (retry.status === 'scheduled') {
    return t('chatBox.modelRetryScheduled', {
      attempt: retry.attempt,
      max: retry.maxAttempts,
      reason: retry.reason || 'unknown',
      delay: ((retry.delayMs ?? 0) / 1000).toFixed(1)
    });
  }
  if (retry.status === 'succeeded') {
    return t('chatBox.modelRetrySucceeded', { count: retry.retries });
  }
  return t('chatBox.modelRetryExhausted', { max: retry.retries });
};

const emit = defineEmits<{
  (e: 'reach-top'): void;
  (e: 'release-head', messageIds: number[]): void;
  (e: 'selection-add', payload: SelectionPayload): void;
  (e: 'selection-side-chat', payload: SelectionPayload): void;
  (e: 'edit-message', message: MessageItem): void;
  (e: 'continue-generation', turnNum: number): void;
}>();

/**
 * Whether this row offers "edit and re-run".
 *
 * Only PERSISTED user messages (a positive row id): the streaming pair carries
 * temporary negative ids, and its turn is not written yet. The composer is
 * disabled while an approval card owns it, and editing under a pending approval
 * would interleave two transcripts.
 * @param message Candidate row.
 */
const canEditMessage = (message: MessageItem): boolean =>
  props.canEdit && props.selectionEnabled && message.role === CHAT_ROLE.USER && message.id > 0 && message.turn_num > 0;

/** The `[interrupted]` marker the backend prefixes onto a cut-off answer. */
const INTERRUPTED_MARKER = /^\[interrupted(?::[a-z_]+)?\]/i;

/**
 * The turn number of the answer that can be continued, or null.
 *
 * Only the LAST message of the list qualifies: a continuation appends to the
 * interrupted answer, so a later row means the transcript moved on. Two sources
 * agree: the live `resumable` flag (terminal frame) and the persisted
 * `[interrupted]` marker prefix (after a reload the flag is recomputed from the
 * content). A streaming row keeps its button hidden until the turn settles.
 */
const continueTurn = computed<number | null>(() => {
  const list = props.messages ?? [];
  const last = list[list.length - 1];
  if (!last || last.role !== CHAT_ROLE.AI || last.streaming) return null;
  const flagged = last.resumable === true || INTERRUPTED_MARKER.test(last.content.trimStart());
  return flagged ? last.turn_num : null;
});

/** The one selection menu instance (a per-row popup would mount one per message). */
const selectionMenuRef = useTemplateRef<{ show: (event: Event) => void }>('selectionMenuRef');
const selectionMenuItems = ref<Array<{ label: string; icon: string; command: () => void }>>([]);

/**
 * Right-click on the transcript: quote the selected text when the selection is
 * valid, otherwise leave the native menu alone (copy/paste still work).
 * @param event
 */
function onMessageContextMenu(event: MouseEvent): void {
  if (!props.selectionEnabled) return;
  const payload = inspectSelection(scrollContainerRef.value);
  if (!payload) return;

  event.preventDefault();
  selectionMenuItems.value = [
    {
      label: t('chat.selection.addToChat'),
      icon: 'pi pi-comment',
      command: () => emit('selection-add', payload)
    }
  ];
  if (props.allowSideChat) {
    selectionMenuItems.value.push({
      label: t('chat.selection.askInSideChat'),
      icon: 'pi pi-comments',
      command: () => emit('selection-side-chat', payload)
    });
  }
  selectionMenuRef.value?.show(event);
}

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
  expandedTurnHistory,
  toggleToolCard,
  toggleThinking,
  toggleCarrier,
  toggleTurnHistory,
  toggleLongMessage
} = useChatCardExpansion();

/**
 * Fold classification for one virtual row's assistant work group.
 * @param rowIndex Virtual row index (the group is `rowGroup(rowIndex)`).
 */
const groupFold = (rowIndex: number): TurnFoldView => classifyTurnFold(regularMessages(rowGroup(rowIndex)));

/**
 * Rows rendered for one group: everything when the group is not foldable or is
 * expanded, otherwise only the settled final answer.
 * @param group Turn group in render order.
 */
const groupMessages = (group: MessageItem[]): MessageItem[] => {
  const rows = regularMessages(group);
  const fold = classifyTurnFold(rows);
  if (!fold.eligible || expandedTurnHistory.has(fold.key)) return rows;
  return fold.finalMessage ? [fold.finalMessage] : rows;
};

/**
 * Whether a row is the final answer of a COLLAPSED fold. Its reasoning moves
 * into the hidden history, so the visible answer renders without the thinking
 * block (expanding the fold brings it back).
 * @param message Row being rendered.
 * @param group Its turn group.
 */
const isFoldedFinal = (message: MessageItem, group: MessageItem[]): boolean => {
  const fold = classifyTurnFold(regularMessages(group));
  return fold.eligible && !expandedTurnHistory.has(fold.key) && fold.finalMessage?.id === message.id;
};

/**
 * Summary shown on the fold trigger: what happened, plus the measured tool time
 * when the backend reported one.
 * @param fold Classified turn.
 */
const foldLabel = (fold: TurnFoldView): string => {
  const parts: string[] = [];
  if (fold.toolCount > 0) parts.push(t('chatBox.turnHistoryTools', { n: fold.toolCount }));
  else if (fold.hasReasoning) parts.push(t('chatBox.turnHistoryThinking'));
  else parts.push(t('chatBox.turnHistoryWork'));
  const duration = formatToolDuration(fold.durationMs);
  if (duration) parts.push(duration);
  return parts.join(' · ');
};

/**
 * Header label of a neutral (injector-origin) card, by the row's `origin`.
 *
 * Injected rows are not the user's words and not the assistant's reply: the
 * backend tags each producer with an origin (`subagent_completion`,
 * `task_intent`, `quality_gate`, `todo_continuation`, `project_dir`, ...) and
 * the card says which one it was. An unknown non-user origin still renders
 * neutrally, under the generic label.
 */
const ORIGIN_LABEL_KEYS: Record<string, string> = {
  subagent_completion: 'chat.backgroundMessage',
  task_intent: 'chat.originTaskIntent',
  quality_gate: 'chat.originQualityGate',
  todo_continuation: 'chat.originTodoNudge',
  project_dir: 'chat.originProjectDir',
  git_head: 'chat.originGitHead'
};

/** Glyph per origin; the default (background tasks) is the server icon. */
const ORIGIN_ICONS: Record<string, string> = {
  project_dir: 'pi pi-folder',
  git_head: 'pi pi-code-branch'
};

/**
 * Header glyph of a neutral card, by the row's `origin`.
 * @param message Injected row (non-user origin).
 */
const originIcon = (message: MessageItem): string => ORIGIN_ICONS[message.origin ?? ''] ?? 'pi pi-server';

/**
 * Header text of a neutral card.
 * @param message Injected row (non-user origin).
 */
const originLabel = (message: MessageItem): string => t(ORIGIN_LABEL_KEYS[message.origin ?? ''] ?? 'chat.originSystem');

//: What a STREAMING bubble renders at most: the tail's characters and lines.
const STREAM_TAIL_CHARS = 4000;
const STREAM_TAIL_LINES = 200;
//: A settled answer past this many CHARACTERS renders a head preview plus an
//: expand control (2500 — was 4000; a mid-length answer still costs the layout a
//: full re-flow on every re-render, and the full text stays one click away).
const LONG_MESSAGE_CHARS = 2500;
//: The preview's size in CHARACTERS, not lines: a "20 lines" preview is a
//: handful of characters for a column of short numbers and thousands of
//: characters for prose, so the cap has to be the thing that actually bounds
//: the rendered block. Every symbol and space counts (`String.length`).
const LONG_MESSAGE_PREVIEW_CHARS = 1000;
//: The preview's row guard, for the same reason the streaming tail has one: a
//: thousand characters of one-digit lines is a thousand laid-out rows.
const LONG_MESSAGE_PREVIEW_LINES = 60;

/** How a bubble renders one message: the text, and which control it needs. */
interface BubbleView {
  /** The markdown source this render shows. */
  text: string;
  /** Streaming tail-only preview (shows the 流式 "streaming" hint line). */
  truncated: boolean;
  /** Settled long answer shown collapsed (shows the 展开全文 "expand" button). */
  collapsed: boolean;
  /** Total character count (symbols and spaces included), for the expand label. */
  chars: number;
}

/**
 * The text a bubble renders for one message.
 *
 * Streaming rows render only their TAIL once the answer grows past the cap: the
 * row is re-laid-out on every flush, and one enormous row starves the page
 * (measured on a 2000-line answer: menus, timers and fetches all froze for tens
 * of seconds until the page caught up). Settled rows render a head PREVIEW and
 * an expand-full-text control instead — the starvation comes from having multi-thousand-
 * character rows at all, and the window usually holds several of them, so the cap
 * has to apply after the turn ends too (the same collapse idiom the tool cards
 * and thinking blocks already use). Both caps count CHARACTERS, symbols and
 * spaces included, because that is what bounds the rendered block: "20 lines" is
 * a few characters for a column of short numbers and thousands for prose.
 * Nothing is lost either way: `message.content` keeps the full text (copy
 * button, tooltips, history all read it).
 * @param message
 */
const bubbleView = (message: MessageItem): BubbleView => {
  const content = message.content;
  const totalChars = content.length;
  if (message.streaming) {
    if (totalChars <= STREAM_TAIL_CHARS) {
      return { text: content, truncated: false, collapsed: false, chars: totalChars };
    }
    let tail = content.slice(-STREAM_TAIL_CHARS);
    // Start at a line boundary so the markdown structure of the tail is intact.
    const firstBreak = tail.indexOf('\n');
    if (firstBreak >= 0) tail = tail.slice(firstBreak + 1);
    // The character cap bounds the block; this one bounds the ROW count, which
    // is what actually starves layout (4000 characters of one-digit lines is
    // 2000 rows).
    const tailLines = tail.split('\n');
    if (tailLines.length > STREAM_TAIL_LINES) tail = tailLines.slice(-STREAM_TAIL_LINES).join('\n');
    return { text: tail, truncated: true, collapsed: false, chars: totalChars };
  }
  if (totalChars > LONG_MESSAGE_CHARS && !expandedLongMessages.has(message.id)) {
    const previewLines = content.slice(0, LONG_MESSAGE_PREVIEW_CHARS).split('\n');
    const preview =
      previewLines.length > LONG_MESSAGE_PREVIEW_LINES
        ? previewLines.slice(0, LONG_MESSAGE_PREVIEW_LINES).join('\n')
        : previewLines.join('\n');
    return { text: preview, truncated: false, collapsed: true, chars: totalChars };
  }
  return { text: content, truncated: false, collapsed: false, chars: totalChars };
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
