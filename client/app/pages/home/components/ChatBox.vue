<template>
  <!-- Floating layer anchor: the wrapper is relatively positioned and hosts the "scroll to bottom"
       floating button (absolutely positioned at the bottom center of the chat list) -->
  <div class="relative flex flex-col flex-1 min-h-0">
    <!-- [scrollbar-gutter:stable]: always reserves a gutter for the (classic) scrollbar,
         preventing content from shifting horizontally when switching between the no-scrollbar and
         scrollbar states (e.g. empty state "start a new conversation" → messages accumulate) -->
    <div
      ref="scrollContainerRef"
      class="flex flex-col gap-6 flex-1 min-h-0 border-b border-solid border-gray-light dark:border-gray-dark overflow-auto px-6 py-4 [scrollbar-gutter:stable]"
      @scroll="updateScrollBottomBtn">
      <div
        v-for="group in turnGroups"
        :key="group[0]?.id"
        :class="['flex flex-col min-w-0', { 'gap-3': turnSpacingClass(group) }]">
        <div
          v-for="message in group"
          :key="message.id"
          :class="[
            'flex justify-start gap-3 min-w-0',
            { 'flex-row-reverse text-right': message.role === CHAT_ROLE.USER },
            { 'text-left': message.role === CHAT_ROLE.AI }
          ]">
          <div
            class="flex justify-center items-center w-10 h-10 rounded-full overflow-hidden shrink-0 bg-gray-100 dark:bg-gray-800">
            <!-- Avatar area: consecutive messages and tool calls all hide the avatar (hidden img,
             consistent with the consecutive-message rendering) -->
            <img
              v-if="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
              :class="[
                'w-full h-full object-cover',
                { hidden: isConsecutive(message.id) || message.role === CHAT_ROLE.TOOL }
              ]"
              :src="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
              :alt="message.role === CHAT_ROLE.USER ? resolvedUserName : resolvedAiName" />
            <span
              v-else
              :class="['pi pi-user', { hidden: isConsecutive(message.id) }]"></span>
          </div>
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
            <div
              v-if="message.role === CHAT_ROLE.AI && message.reasoning"
              :class="[
                'w-fit mb-1 text-sm transition-colors duration-200',
                { 'rounded-xl': isConsecutive(message.id) }
              ]">
              <button
                type="button"
                :class="[
                  'flex items-center gap-2 w-full text-left cursor-pointer select-none px-3 py-1.5 text-xs font-medium rounded-md border border-solid transition-colors duration-200',
                  expandedThinking.has(message.id)
                    ? 'bg-gray-50 dark:bg-gray-800/60 border-gray-200 dark:border-gray-600 text-gray-500 dark:text-gray-400'
                    : 'bg-gray-50/60 dark:bg-gray-800/30 border-gray-100 dark:border-gray-700/60 text-[#6B7280] dark:text-[#9CA3AF]'
                ]"
                @click="toggleThinking(message.id)">
                <span class="pi pi-brain text-xs"></span>
                <span>{{ t('chatBox.thinking') }}</span>
                <span
                  :class="[
                    'pi pi-chevron-down text-xs ml-auto transition-transform duration-200',
                    { 'rotate-180': expandedThinking.has(message.id) }
                  ]"></span>
              </button>
              <div
                v-if="expandedThinking.has(message.id)"
                class="mt-1 px-3 py-2 text-xs whitespace-pre-wrap break-words leading-relaxed text-gray-500 dark:text-gray-400 border-l-2 border-solid border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/30 rounded-r-md">
                {{ message.reasoning }}
              </div>
            </div>
            <!-- Tool call card -->
            <div
              v-if="message.role === CHAT_ROLE.TOOL"
              class="flex flex-col gap-2 w-full px-3 py-2 rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60 text-sm text-gray-600 dark:text-gray-300 overflow-x-auto">
              <button
                type="button"
                class="flex items-center gap-2 w-full text-left cursor-pointer select-none"
                @click="toggleToolCard(message.id)">
                <span class="pi pi-hammer text-xs"></span>
                <span class="font-medium">{{ message.toolName }}</span>
                <span
                  v-if="message.toolStatus === 'running'"
                  class="pi pi-spin pi-spinner text-xs text-blue-500"></span>
                <span
                  v-else-if="message.toolStatus === 'failed' || message.toolStatus === 'error'"
                  class="pi pi-times text-xs text-red-500"></span>
                <span
                  v-else
                  class="pi pi-check text-xs text-green-500"></span>
                <span
                  v-if="isToolMessage(message)"
                  :class="[
                    'pi pi-chevron-down text-xs ml-auto transition-transform duration-200',
                    { 'rotate-180': expandedToolCards.has(message.id) }
                  ]"></span>
              </button>
              <!-- Expanded details: args + result (expanding to view live args/progress is also
               allowed while the tool is running) -->
              <div
                v-if="expandedToolCards.has(message.id)"
                class="flex flex-col gap-2 border-t border-solid border-gray-200 dark:border-gray-600 pt-2">
                <div v-if="message.toolArgs && Object.keys(message.toolArgs).length">
                  <div class="text-xs font-semibold mb-1">{{ t('chatBox.toolArgs') }}</div>
                  <pre
                    class="text-xs whitespace-pre-wrap break-words bg-white dark:bg-gray-900/60 rounded p-2 border border-solid border-gray-200 dark:border-gray-600"
                    >{{ formatToolArgs(message.toolArgs) }}</pre>
                </div>
                <div v-if="message.toolResult">
                  <div class="text-xs font-semibold mb-1">{{ t('chatBox.toolResult') }}</div>
                  <pre
                    class="text-xs whitespace-pre-wrap break-words bg-white dark:bg-gray-900/60 rounded p-2 border border-solid border-gray-200 dark:border-gray-600"
                    >{{ message.toolResult }}</pre>
                </div>
                <div
                  v-if="message.toolStatus === 'running' && !message.toolResult"
                  class="flex items-center gap-2 text-xs text-blue-500 dark:text-blue-400">
                  <span class="pi pi-spin pi-spinner text-xs"></span>{{ t('chatBox.toolRunning') }}
                </div>
                <div
                  v-else-if="!message.toolResult && !(message.toolArgs && Object.keys(message.toolArgs).length)"
                  class="text-xs text-gray-400 dark:text-gray-500">
                  {{ t('chatBox.toolNoOutput') }}
                </div>
              </div>
            </div>
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
              <!-- Copy message button: shown only for user/AI text messages with a non-empty body;
               fades in when the bubble is hovered or keyboard-focused -->
              <button
                v-if="canCopyMessage(message)"
                type="button"
                :class="[
                  'absolute top-2 right-2 flex items-center justify-center w-7 h-7 rounded-full border border-solid cursor-pointer select-none opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity duration-200',
                  message.role === CHAT_ROLE.USER
                    ? 'bg-blue-700/40 hover:bg-blue-600/50 border-white/20 text-blue-100 hover:text-white' /* Floating button on the blue user bubble: translucent light white */
                    : 'bg-white/70 hover:bg-white border-gray-200 shadow-sm text-gray-400 hover:text-gray-600 dark:border-gray-700 dark:bg-gray-800/60 dark:hover:bg-gray-700 dark:text-gray-400 dark:hover:text-gray-200' /* Floating button on the white AI bubble: light gray */
                ]"
                :aria-label="t('chatBox.copy')"
                :title="copiedMessageId === message.id ? t('chatBox.copied') : t('chatBox.copy')"
                @click="copyMessage(message)">
                <span
                  :class="[
                    'pi text-xs',
                    copiedMessageId === message.id
                      ? message.role === CHAT_ROLE.USER
                        ? 'pi-check text-emerald-300'
                        : 'pi-check text-green-600'
                      : 'pi-copy'
                  ]"></span>
              </button>
              <!-- The v-safe-html directive handles markdown rendering + DOMPurify allowlist
               sanitization internally (app/directives/safeHtml.ts) -->
              <div v-safe-html="message.content"></div>
              <template v-if="messageImages(message).length">
                <div class="flex flex-wrap gap-2 mt-2">
                  <template
                    v-for="(src, i) in messageImages(message)"
                    :key="i">
                    <!-- Media referenced by historical messages may no longer exist on disk (rows
                     written before the media feature landed); on load failure, hide the broken
                     image and show a placeholder block instead of a broken-image icon. -->
                    <img
                      v-if="!failedImageSources.has(resolveImageSrc(message, src))"
                      :src="resolveImageSrc(message, src)"
                      class="w-24 h-24 object-cover rounded-lg border border-solid border-gray-200 cursor-pointer hover:opacity-80 transition-opacity duration-200"
                      role="button"
                      tabindex="0"
                      :aria-label="t('a11y.previewImage')"
                      @click="openPreview(resolveImageSrc(message, src))"
                      @keydown.enter.prevent="openPreview(resolveImageSrc(message, src))"
                      @keydown.space.prevent="openPreview(resolveImageSrc(message, src))"
                      @error="onImageError($event, resolveImageSrc(message, src))" />
                    <div
                      v-else
                      class="w-24 h-24 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 flex items-center justify-center text-xs text-gray-400 dark:text-gray-500">
                      {{ t('chatBox.imageLoadFailed') }}
                    </div>
                  </template>
                </div>
              </template>
              <!-- Audio attachments -->
              <template v-if="messageAudios(message).length">
                <div class="flex flex-col gap-2 mt-2 min-w-[200px] max-w-full">
                  <audio
                    v-for="(src, i) in messageAudios(message)"
                    :key="i"
                    :src="resolveAudioSrc(message, src)"
                    controls
                    preload="metadata"
                    class="w-full max-w-[280px] rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60" />
                </div>
              </template>
              <!-- Video attachments -->
              <template v-if="messageVideos(message).length">
                <div class="flex flex-col gap-2 mt-2 max-w-full">
                  <video
                    v-for="(src, i) in messageVideos(message)"
                    :key="i"
                    :src="resolveVideoSrc(message, src)"
                    controls
                    preload="metadata"
                    class="max-w-[280px] max-h-56 rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-black" />
                </div>
              </template>
            </div>
            <!-- Model metadata (model name + token usage; shown only for AI messages when the fields exist) -->
            <div
              v-if="
                message.role === CHAT_ROLE.AI &&
                (message.modelName || message.inputTokens !== undefined || message.outputTokens !== undefined)
              "
              class="mt-1 text-xs text-[#9CA3AF] dark:text-[#6B7280]">
              <template v-if="message.modelName">{{ message.modelName }}</template>
              <template v-if="message.inputTokens !== undefined || message.outputTokens !== undefined">
                <template v-if="message.modelName"> · </template>
                {{
                  t('chatBox.modelMeta', {
                    input: message.inputTokens ?? 0,
                    output: message.outputTokens ?? 0
                  })
                }}
              </template>
            </div>
          </div>
        </div>
      </div>
    </div>

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
// Components

// Methods/types
import type { MessageItem } from '../type';
import { CHAT_ROLE } from '../type';
import { formatCompactTimeString } from '@/common/utils';
import { useI18n } from 'vue-i18n';
import { vSafeHtml } from '@/directives/safeHtml';

const { t } = useI18n();

interface Props {
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
  aiName: ''
});

/** User display name: falls back to the i18n default when the prop is empty */
const resolvedUserName = computed(() => props.userName || t('chatBox.defaultUserName'));
/** AI display name: falls back to the i18n default when the prop is empty */
const resolvedAiName = computed(() => props.aiName || t('chatBox.defaultAiName'));

// Image preview
const { openPreview } = useImagePreview();

const filteredMessages = computed(() => {
  return props.messages.filter((item: MessageItem) => {
    // Hide "AI empty placeholder" messages: right after sending, when the AI has not produced
    // any content yet (no tool calls, no thinking content either), do not render this placeholder
    // bubble containing only a name + an empty box, so "Sherry" does not look glued to a white box.
    // But empty-body messages carrying reasoning must pass through: the thinking bubble uses them
    // as its host, otherwise the model thinking block would be filtered out together with the
    // empty placeholder and the thinking bubble could never render.
    if (item.role === CHAT_ROLE.AI && !item.content.trim() && !item.reasoning) {
      return false;
    }
    return true;
  });
});

// [DIAG-CHATBOX] Probe removed —— the reasoning rendering chain has been verified (root cause was an i/span selector mismatch).

/**
 * Determine whether a message should be rendered as a "consecutive message"
 * (no avatar shown, compact spacing, square-cornered bubbles touching each other).
 *
 * The check must be performed on the **original (unfiltered)** message sequence, not on
 * `filteredMessages`: filteredMessages drops "AI empty placeholder" messages, but an empty
 * placeholder is a real turn boundary (handleSend appends an empty AI placeholder after every
 * user message). If adjacency-by-same-role were judged on the filtered list, the AI placeholder
 * in [userA, AI empty placeholder, userB] would be removed, making userB be misjudged as a
 * consecutive message of the preceding userA —— exactly the root cause of "the first message
 * sent after opening the page was treated as a consecutive message".
 *
 * Correct semantics: skip TOOL rows in the original sequence and only check whether the nearest
 * preceding visible message has the same role. An empty AI placeholder still keeps the `ai` role,
 * which differs from the user role, so it naturally acts as a turn separator; multiple same-role
 * rows within one turn (e.g. tool calls + the final reply inside a single AI turn) are still
 * correctly judged as consecutive.
 */
const consecutiveIdSet = computed(() => {
  const result = new Set<number>();
  let prevRole: CHAT_ROLE | null = null;
  for (const item of props.messages) {
    if (item.role === CHAT_ROLE.TOOL) {
      continue;
    }
    if (prevRole === item.role) {
      result.add(item.id);
    }
    prevRole = item.role;
  }
  return result;
});
const isConsecutive = (id: number) => consecutiveIdSet.value.has(id);

/**
 * Group the rendered messages by "turn" to implement segmented spacing:
 *  - User messages (USER) each form their own group: both neighbors are turn boundaries, with
 *    the outer gap-6 (24px) providing the separation;
 *  - Consecutive AI/TOOL rows after the first message are grouped together: the group is
 *    tightened with gap-3 (12px) (bubble↔tool card↔bubble compactly joined, including
 *    tool-call spans).
 *
 * This way the spacing between two adjacent rows meets the requirement: only "AI/TOOL → AI/TOOL"
 * gets 12px, while any boundary involving USER (user→AI, AI→user, user→user) gets the outer
 * 24px role-switch spacing.
 *
 * Grouping is based on the **render order** (filteredMessages): empty AI placeholder messages
 * have already been filtered out, so `user → AI placeholder (filtered) → user` become directly
 * adjacent in render order; the second user row correctly starts a new turn, preventing the next
 * AI message from being merged into the previous turn by mistake.
 */
const turnGroups = computed<MessageItem[][]>(() => {
  const groups: MessageItem[][] = [];
  for (const item of filteredMessages.value) {
    const last = groups.length ? groups[groups.length - 1] : null;
    const prevRole = last ? (last[last.length - 1]?.role ?? null) : null;
    // New turn: first message, this row is a user (always forms its own group), or the previous
    // row is a user (separating the AI reply from the user bubble)
    if (item.role === CHAT_ROLE.USER || groups.length === 0 || prevRole === CHAT_ROLE.USER) {
      groups.push([item]);
    } else {
      // Consecutive AI/TOOL rows → merge into the last non-user group
      groups[groups.length - 1]?.push(item);
    }
  }
  return groups;
});

/**
 * Determine whether a turn group should use the 12px inner spacing (flex gap-3).
 * Only groups that are "multi-row with a non-user first row" (pure AI/TOOL consecutive rows)
 * use gap-3; single-row groups or groups containing a user do not need it — their spacing is
 * provided by the outer gap-6 (24px).
 */
const turnSpacingClass = (group: MessageItem[]): boolean => group.length > 1 && group[0]?.role !== CHAT_ROLE.USER;

/** Chat list scroll container (the outermost overflow-auto div), used for auto-scrolling to the bottom */
const scrollContainerRef = useTemplateRef<HTMLDivElement>('scrollContainerRef');

/** "Near bottom" threshold (px): a distance to the bottom within this value means the user is still following the latest messages */
const NEAR_BOTTOM_THRESHOLD = 80;

/**
 * Determine whether the user is currently near the bottom of the list.
 *
 * Measured before the DOM update (watch defaults to pre flush), so what is read is the scroll
 * state before this change has rendered.
 */
const isNearBottom = (): boolean => {
  const el = scrollContainerRef.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD;
};

/**
 * Scroll the chat list to the bottom (making new messages visible).
 *
 * scrollHeight must be read after the DOM update (nextTick); otherwise the measured value is the
 * old height and the scroll cannot reach the bottom of the newest messages. The parent component
 * home/index/[sid].vue reassigns the messages array (new reference) every time a streaming chunk
 * arrives, so watching the reference change covers all three scenarios: "first page load",
 * "sending a message", and "each AI reply chunk".
 */
const scrollToBottom = () => {
  nextTick(() => {
    const el = scrollContainerRef.value;
    if (el) {
      el.scrollTop = el.scrollHeight;
      updateScrollBottomBtn();
    }
  });
};

/** "Scroll to bottom" floating button visibility: shown when the scroll position is more than NEAR_BOTTOM_THRESHOLD (80px) from the bottom */
const showScrollBottom = ref(false);

/**
 * Sync the "scroll to bottom" button visibility on scroll (triggered by the scroll container's
 * @scroll). The programmatic scroll in scrollToBottom also dispatches a scroll event, so the
 * button hides accordingly.
 */
const updateScrollBottomBtn = () => {
  const el = scrollContainerRef.value;
  if (!el) return;
  showScrollBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight > NEAR_BOTTOM_THRESHOLD;
};

/**
 * Scrolling strategy after the message list changes:
 * - New user messages added (sending a message / loading a history session): always scroll to
 *   the bottom;
 * - Other changes (AI streaming chunk-by-chunk appends, tool events, etc.): follow only while
 *   the user is still near the bottom, preventing streaming output from yanking the user back
 *   down while they scroll up to review history.
 */
watch(
  () => props.messages,
  (msgs, oldMsgs) => {
    const added = (msgs ?? []).slice(oldMsgs?.length ?? 0);
    if (added.some(m => m.role === CHAT_ROLE.USER) || isNearBottom()) {
      scrollToBottom();
    }
  }
);

// After the component mounts (first page open), scroll to the bottom so the latest messages are visible
onMounted(() => scrollToBottom());

/** Backend /media endpoint root: derived from VITE_API_BACK_URL (trailing slashes stripped) */
const backendBaseUrl = ((import.meta.env.VITE_API_BACK_URL as string) ?? '').replace(/\/+$/, '');

/**
 * Resolve the image entries in a message into renderable <img src> values.
 * Semantics (see the MessageItem.images comment in type.ts):
 *  - User messages: raw base64 (without the data: prefix) → assembled locally into
 *    data:image/*;base64,<data>
 *  - AI messages: persisted absolute file paths → served via the backend /media endpoint, which
 *    returns the image by session_id + filename; the original basename (e.g. <ts>.png) must be
 *    taken, dropping the directory part of the file path.
 * Decision basis: a file path necessarily contains a backslash \ or ends with a common media
 * extension; the base64 alphabet happens to contain / and + (and is usually padded with =),
 * so "/" must never be used as the "file path" test —— that would misjudge the user's raw
 * base64 image as a /media request (the root cause of 4 historical "media not found" bugs).
 */
const resolveImageSrc = (message: MessageItem, entry: string): string => {
  const s = (entry ?? '').trim();
  if (!s) return '';
  // Absolute URLs (http/https) pass through as-is: user images injected by the middleware are
  // already-served http(s)://…/images/<hash>.png addresses, which must be rendered verbatim;
  // do not misjudge them as /media file paths by extension (otherwise 404 / broken image).
  if (/^https?:\/\//i.test(s)) return s;
  const isFilePath = s.includes('\\') || /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(s);
  if (isFilePath) {
    // AI messages: fetch via /media; the file may carry any directory prefix, so take its basename
    const filename = s.split(/[\\/]/).pop() || '';
    return `${backendBaseUrl}/media?session_id=${encodeURIComponent(message.session_id ?? '')}&filename=${encodeURIComponent(filename)}`;
  }
  // User messages: local base64
  return `data:image/*;base64,${s}`;
};

/**
 * Extract image URLs injected by the multimodal processor into message content.
 *
 * The middleware persists a marker like:
 *   "[System: The user uploaded N image(s). Location: http://…/images/<hash>.png,…]"
 * This fallback resolves those served URLs so history/legacy rows whose
 * `images` field is empty still render their images.
 */
const extractContentImageUrls = (content: string): string[] => {
  if (!content) return [];
  const m = content.match(/Location:\s*([^\]\n]+)/);
  if (!m) return [];
  return (m[1] ?? '')
    .split(/[,\s]+/)
    .map(u => u.replace(/[\]\s.,!;:]+$/g, ''))
    .filter(u => /^https?:\/\//i.test(u));
};

/**
 * Images to render for a message: explicit `images` wins; otherwise fall back
 * to URLs parsed from the content's Location marker.
 */
const messageImages = (message: MessageItem): string[] => {
  const explicit = message.images ?? [];
  return explicit.length > 0 ? explicit : extractContentImageUrls(message.content);
};

/**
 * Resolve the audio/video entries in a message into playable src values.
 * Semantics are identical to `resolveImageSrc` (user messages → local base64; AI messages →
 * /media file paths):
 *  - User messages: raw base64 (without the data: prefix) → assembled locally into
 *    data:audio/*;base64,<data> or data:video/*;base64,<data>
 *  - AI messages: persisted absolute file paths → fetched via the backend /media endpoint, with
 *    the basename used to build the URL
 *  - Absolute http(s):// URLs pass through as-is
 */
const resolveMediaSrc = (message: MessageItem, entry: string, mimePrefix: string): string => {
  const s = (entry ?? '').trim();
  if (!s) return '';
  // Absolute URLs (http/https) pass through as-is: media addresses injected by the middleware start with http(s)://
  if (/^https?:\/\//i.test(s)) return s;
  const isFilePath = s.includes('\\') || /\.(mp3|wav|ogg|m4a|aac|flac|mp4|webm|mov|avi|mkv|m4v)$/i.test(s);
  if (isFilePath) {
    // AI messages: fetch via /media; the file may carry any directory prefix, so take its basename
    const filename = s.split(/[\\/]/).pop() || '';
    return `${backendBaseUrl}/media?session_id=${encodeURIComponent(message.session_id ?? '')}&filename=${encodeURIComponent(filename)}`;
  }
  // User messages: local base64 (data:<mimePrefix>;base64,<data>)
  return `data:${mimePrefix};base64,${s}`;
};

/** Resolve the audio src (mime prefix audio/*) */
const resolveAudioSrc = (message: MessageItem, entry: string): string => resolveMediaSrc(message, entry, 'audio/*');

/** Resolve the video src (mime prefix video/*) */
const resolveVideoSrc = (message: MessageItem, entry: string): string => resolveMediaSrc(message, entry, 'video/*');

/** Audio/video entries carried by this message */
const messageAudios = (message: MessageItem): string[] => message.audios ?? [];
const messageVideos = (message: MessageItem): string[] => message.videos ?? [];

/**
 * Set of image srcs that failed to load (e.g. the /media file referenced by a historical message
 * no longer exists on disk → 404). Once an src fails to load it is recorded here; later
 * re-renders no longer attempt to load that src and directly show the placeholder block instead.
 */
const failedImageSources = reactive(new Set<string>());

/** Callback for <img> load failures (including 404/network errors): record the failed src in the set to hide the broken image. */
const onImageError = (event: Event, src: string) => {
  if (src) {
    failedImageSources.add(src);
  }
};

// ── Tool call card expand/collapse ────────────────────────────────

/** Set of tool-card message ids currently expanded (collapsed by default) */
const expandedToolCards = reactive(new Set<number>());

/** Toggle the expand/collapse state of a tool card */
const toggleToolCard = (id: number) => {
  if (expandedToolCards.has(id)) {
    expandedToolCards.delete(id);
  } else {
    expandedToolCards.add(id);
  }
};

// ── Model thinking/reasoning block expand/collapse ─────────────────────────────

/** Set of thinking-block message ids currently expanded (collapsed by default) */
const expandedThinking = reactive(new Set<number>());

/** Toggle the expand/collapse state of a message's thinking block */
const toggleThinking = (id: number) => {
  if (expandedThinking.has(id)) {
    expandedThinking.delete(id);
  } else {
    expandedThinking.add(id);
  }
};

/** Whether this message is a tool call card (tool cards are always expandable; live args/progress can be viewed even while running) */
const isToolMessage = (message: MessageItem): boolean => {
  return message.role === CHAT_ROLE.TOOL && !!message.toolName;
};

/** Format the tool args object into readable JSON text */
const formatToolArgs = (args: Record<string, unknown>): string => {
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
};

// ── Copy message body ─────────────────────────────────────────

/** Id of the message currently showing the "copied ✓" feedback (at most one at a time, preventing multiple bubbles from flashing simultaneously) */
const copiedMessageId = ref<number | null>(null);

/** Handle of the copy-feedback reset timer (must be cleared on re-click or component unmount, preventing an old timer from wiping the new feedback early) */
let copyResetTimer: ReturnType<typeof setTimeout> | null = null;

/** Whether this message shows a copy button: only user/AI text messages with a non-empty body (tool cards and empty messages are excluded from the copy logic) */
const canCopyMessage = (message: MessageItem): boolean => {
  return (
    (message.role === CHAT_ROLE.USER || message.role === CHAT_ROLE.AI) &&
    !!message.content &&
    message.content.trim().length > 0
  );
};

/**
 * Write text to the clipboard.
 * Prefers the modern Clipboard API (requires a secure context); when it is unavailable or
 * rejects, fall back to "hidden textarea + document.execCommand('copy')". Returns false when
 * all paths fail; the caller only logs a warning.
 */
const copyTextToClipboard = async (text: string): Promise<boolean> => {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Clipboard API rejected/errored → use the fallback below
    }
  }
  return fallbackCopyText(text);
};

/** Fallback copy: hidden textarea + execCommand('copy') (safety net for legacy environments / non-secure contexts) */
const fallbackCopyText = (text: string): boolean => {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  // Positioned off-screen and invisible, avoiding layout jumps or flicker
  textarea.style.position = 'fixed';
  textarea.style.top = '-9999px';
  textarea.style.left = '-9999px';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    // execCommand may throw: keep ok=false, treating it as a copy failure
  }
  document.body.removeChild(textarea);
  return ok;
};

/** Copy a message's raw Markdown body; on success briefly show the ✓ feedback (reverting to the copy icon after 1500ms) */
const copyMessage = async (message: MessageItem) => {
  // Clear the previous feedback timer so that with rapid clicks only the latest feedback takes effect
  if (copyResetTimer) {
    clearTimeout(copyResetTimer);
    copyResetTimer = null;
  }
  const ok = await copyTextToClipboard(message.content ?? '');
  if (ok) {
    copiedMessageId.value = message.id;
    copyResetTimer = setTimeout(() => {
      copiedMessageId.value = null;
      copyResetTimer = null;
    }, 1500);
  } else {
    // Both paths failed: only warn, never throw to the template or interrupt rendering
    console.warn('[ChatBox] 复制消息正文失败，暂不支持剪贴板写入。');
  }
};

// Clear the pending feedback reset timer when the component unmounts
onBeforeUnmount(() => {
  if (copyResetTimer) {
    clearTimeout(copyResetTimer);
    copyResetTimer = null;
  }
});
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
