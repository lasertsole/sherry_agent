<template>
  <div class="flex flex-col flex-1 h-full bg-transparent dark:bg-transparent">
    <!-- Chat main area / empty state (with a session sid shows the chat panel; the root path without sid shows "start a new chat").
        Background tasks live in the toolbar's terminal entry (the third icon's
        popover, TasksButton), so this column is always the chat. -->
    <div
      v-if="sessionId"
      class="flex flex-col flex-1 h-full min-h-0">
      <div class="flex flex-1 flex-col min-h-0">
        <!-- History is still loading and nothing is on screen yet: a chat-shaped
             skeleton, so switching sessions does not flash an empty state. A
             KeepAlive restore already holds its messages and never matches. -->
        <div
          v-if="isHistoryLoading && chatMessages.length === 0"
          class="flex flex-1 flex-col gap-4 overflow-hidden p-4"
          data-test="chat-history-skeleton">
          <div class="flex gap-2">
            <Skeleton
              shape="circle"
              size="2rem" />
            <div class="flex flex-1 flex-col gap-1">
              <Skeleton
                width="30%"
                height="0.7rem" />
              <Skeleton
                width="60%"
                height="0.7rem" />
            </div>
          </div>
          <div class="flex flex-row-reverse gap-2">
            <Skeleton
              shape="circle"
              size="2rem" />
            <div class="flex flex-1 flex-col items-end gap-1">
              <Skeleton
                width="40%"
                height="0.7rem" />
              <Skeleton
                width="50%"
                height="0.7rem" />
            </div>
          </div>
          <div class="flex gap-2">
            <Skeleton
              shape="circle"
              size="2rem" />
            <div class="flex flex-1 flex-col gap-1">
              <Skeleton
                width="25%"
                height="0.7rem" />
              <Skeleton
                width="70%"
                height="0.7rem" />
              <Skeleton
                width="55%"
                height="0.7rem" />
            </div>
          </div>
        </div>
        <ChatBox
          v-else
          ref="chatBoxRef"
          :messages="chatMessages"
          :user-avatar="characterInfo.userAvatar"
          :ai-avatar="characterInfo.aiAvatar"
          :user-name="characterInfo.userName"
          :ai-name="characterInfo.aiName"
          :loading-older="loadingOlder"
          :selection-enabled="!hitlRequest"
          :can-edit="!isSending"
          :can-fork="!isSending && !isForkChild"
          @reach-top="loadOlderHistory"
          @release-head="onReleaseHead"
          @selection-add="onSelectionAdd"
          @selection-side-chat="onSelectionSideChat"
          @edit-message="onEditMessage"
          @continue-generation="handleContinueGeneration"
          @fork-turn="onForkTurn"
          :load-older-history="loadOlderHistory"
          :reset-exhausted="resetExhausted" />
        <!-- Composer dock = EVERYTHING below the transcript: attachment previews,
             the reconnect banner / HITL card / edit banner and the input box. The
             floating conversation measures this block so dragging its lower bound
             can collapse the window to exactly the input area. -->
        <div
          data-test="chat-composer-dock"
          class="flex shrink-0 flex-col">
          <!-- Pending attachment previews (kept separate above the input box, so
               they do not squeeze the h-40 input box) -->
          <PendingMediaTray
            :images="selectedImages"
            :audios="selectedAudios"
            :videos="selectedVideos"
            @remove-image="removeImage"
            @remove-audio="removeAudio"
            @remove-video="removeVideo" />
          <!-- Chat input box area (position:relative parent, used as the anchor for other floating elements) -->
          <div class="relative">
            <!-- WS stream reconnect banner: shown while sendChatMessageWs is in exponential-backoff reconnection (browser mode);
              overlays the toolbar row, disappears automatically after a successful reconnect or a reconnect failure -->
            <Transition name="reconn-fade">
              <div
                v-if="reconnectState"
                class="absolute top-0 left-0 right-0 z-20 flex items-center justify-center gap-2 py-1.5 px-3 text-xs font-medium bg-amber-400/90 text-gray-900 shadow-sm"
                role="status"
                aria-live="polite">
                <i
                  class="pi pi-sync"
                  aria-hidden="true"></i>
                <span>{{
                  t('connection.reconnecting', { attempt: reconnectState.attempt, max: reconnectState.max })
                }}</span>
              </div>
            </Transition>
            <!-- Queue list: one row per message the backend has enqueued for this busy session
              (browser WS mode). Each row shows its position, its text (editable before delivery),
              立刻 (interrupt the running turn and deliver this one next) and cancel. Hidden while a
              HITL decision occupies the input slot — a decision is what unblocks the session. -->
            <Transition name="reconn-fade">
              <QueuedMessageBar
                v-if="queueBadgeList.length > 0 && !hitlRequest"
                :badges="queueBadgeList"
                @update-text="onQueueTextUpdate"
                @edit="editQueuedMessage"
                @send-now="sendNow"
                @cancel="cancelQueuedMessage" />
            </Transition>
            <!-- Chat input box area (fixed h-40, keeping the send button position stable) -->
            <div class="flex flex-col h-40">
              <!-- Chat tools (hidden while a HITL request occupies the input slot).
                 [@container]: with both sidebars open this row narrows to ~280px
                 while its content needs ~472px, so below 500px only the anchors
                 survive — the access-mode list and the project-directory chip
                 (right side); the task badge, the context ring, the model picker
                 and the thinking control hide.
                 `hidden!` is the important form: PrimeVue's own display rules and
                 the sibling `flex` utility are unlayered/later, so the plain
                 variant loses the tie. -->
              <div
                v-show="!hitlRequest"
                data-test="composer-toolbar"
                class="@container h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
                <!-- Access mode: the leftmost anchor, showing the current mode's
                   shield (confirm changes / auto-edit / full access). The media
                   uploads moved into the composer's + menu, so this row no
                   longer carries a second plus button. -->
                <AccessModePicker
                  v-if="mySid"
                  :session-id="mySid" />
                <!-- Running background work of this session (terminal entry): the
                   badge counts sub-agents, foreground tool calls AND detached
                   background commands; the popover lists all three and is where a
                   detached command is watched and stopped.
                   Thinned away on a narrow column (see the row's @container note). -->
                <TasksButton
                  v-if="mySid"
                  class="@max-[500px]:hidden!"
                  :session-id="mySid"
                  :background-tasks="backgroundTasks"
                  :background-running="backgroundTasksRunning"
                  :background-killing="backgroundTasksKilling"
                  :background-kill-refusal="backgroundTasksKillRefusal"
                  :background-reload="loadBackgroundTasks"
                  :background-kill="stopBackgroundTask"
                  @focus="onTasksFocus" />
                <!-- Per-session model + thinking controls (right side of the media
                   toolbar). Switching is allowed at any time; a mid-turn change
                   is parked by the backend and lands on the next turn. -->
                <div
                  v-if="mySid"
                  class="ml-auto flex items-center">
                  <!-- Context ring sits immediately left of the model picker (it
                     reports what that model's window holds). -->
                  <ContextUsageButton
                    class="@max-[500px]:hidden!"
                    :session-id="mySid"
                    :used-tokens="lastPromptTokens" />
                  <SessionModelPicker
                    class="@max-[500px]:hidden!"
                    :session-id="mySid" />
                  <ThinkingToggle
                    class="@max-[500px]:hidden!"
                    :session-id="mySid" />
                  <!-- Project directory: the root every file tool, shell command
                     and path check resolves against. -->
                  <ProjectDirectoryChip :session-id="mySid" />
                </div>
                <!-- Hidden image file input: triggered by the composer + menu's image entry via triggerImagePicker() -->
                <input
                  ref="imageFileInputRef"
                  type="file"
                  accept="image/*"
                  multiple
                  class="hidden"
                  @change="onImageSelected" />
                <!-- Hidden audio file input: triggered by the composer + menu's audio entry via triggerAudioPicker() -->
                <input
                  ref="audioFileInputRef"
                  type="file"
                  accept="audio/*"
                  multiple
                  class="hidden"
                  @change="onAudioSelected" />
                <!-- Hidden video file input: triggered by the composer + menu's video entry via triggerVideoPicker() -->
                <input
                  ref="videoFileInputRef"
                  type="file"
                  accept="video/*"
                  multiple
                  class="hidden"
                  @change="onVideoSelected" />
                <!-- Hidden generic file input: triggered by the composer + menu's
                   "add file" entry (no accept filter — any file becomes an
                   attachment through the same base64 pipeline). -->
                <input
                  ref="fileAttachmentInputRef"
                  type="file"
                  multiple
                  class="hidden"
                  @change="onFileAttachmentSelected" />
              </div>
              <!-- HITL panel occupies the same slot as the input box; the two are mutually exclusive (v-show) -->
              <HitlApprovalCard
                :request="hitlRequest"
                @decide="handleHitlDecision" />
              <!-- Edit mode banner: the composer is about to rewrite a sent
                 user message (and re-run that turn) instead of sending a new
                 one. Esc or ✕ leaves edit mode without touching the transcript. -->
              <div
                v-if="editingTurn"
                class="mx-2 mb-1 flex items-center gap-2 rounded-md border border-solid border-amber-300/70 bg-amber-50 px-3 py-1.5 text-xs text-amber-700 dark:border-amber-700/50 dark:bg-amber-950/30 dark:text-amber-300"
                data-test="chat-edit-banner">
                <i class="pi pi-pencil text-[10px]"></i>
                <span class="flex-1">{{ t('chatBox.editingMessage') }}</span>
                <button
                  type="button"
                  class="underline"
                  data-test="chat-edit-cancel"
                  @click="cancelEditMessage">
                  {{ t('common.cancel') }}
                </button>
              </div>
              <!-- Input box: hidden while a HITL request occupies the same slot -->
              <ChatInputBox
                v-show="!hitlRequest"
                ref="chatInputBoxRef"
                v-model:draft="draft"
                :sending="isSending"
                :disabled="!!hitlRequest"
                :disabled-text="t('chatInput.waitingApproval')"
                :session-id="sessionId"
                @send="onComposerSend"
                @stop="handleStop"
                @upload-image="triggerImagePicker"
                @upload-audio="triggerAudioPicker"
                @upload-video="triggerVideoPicker"
                @upload-file="triggerFilePicker" />
            </div>
          </div>
        </div>
      </div>
    </div>
    <!-- Empty state (root path without sid only): shows a centered "start a new chat" button when there are no messages -->
    <div
      v-else
      class="flex-1 flex flex-col items-center justify-center gap-4">
      <div class="flex flex-col items-center gap-2">
        <span class="pi pi-comments text-4xl text-[#9CA3AF]"></span>
        <p class="text-base font-medium text-[#6B7280] dark:text-[#9CA3AF]">{{ t('history.noSessions') }}</p>
      </div>
      <Button
        icon="pi pi-plus"
        :label="t('toolbar.newChat')"
        @click="handleCreateSession" />
    </div>
  </div>
</template>

<script lang="ts" setup>
// Page-level error capture: runtime errors for all descendant components (ChatBox / HITL card, etc.)
// → logUtil logs + global toast, return false prevents bubbling up to home/index.vue
// (03-errorCaptured factory function pattern)
useErrorCaptured();

// components
import ChatBox from '../components/ChatBox.vue';
import { ChatInputBox } from '#components';
// function
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import type { MessageItem } from '../type.ts';
import type { ChatController } from '@/composables/messages';
import type { FileMentionPayload } from '~/composables/use-file-mention';
import ThinkingToggle from '../components/ThinkingToggle.vue';
import ContextUsageButton from '../components/ContextUsageButton.vue';
import TasksButton from '../components/TasksButton.vue';
import AccessModePicker from '../components/AccessModePicker.vue';
import SessionModelPicker from '../components/SessionModelPicker.vue';
import ProjectDirectoryChip from '../components/ProjectDirectoryChip.vue';
import PendingMediaTray from '../components/PendingMediaTray.vue';
import QueuedMessageBar from '../components/QueuedMessageBar.vue';
import HitlApprovalCard from '~/components/chat/hitl/HitlApprovalCard.vue';
import { useNewSessionStore } from '~/stores/new-session';
import { useTaskflowStore } from '~/stores/taskflow';
import { useTodoStore } from '~/stores/todo';
import { logUtil } from '~/utils/log';

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const localePath = useLocalePath();

/** Current session ID (from the [sid] route param) */
const sessionId = computed(() => String(route.params.sid ?? ''));

/**
 * This instance's 'frozen' session ID: Each [sid].vue instance is independently cached by KeepAlive using page-key (=sid),
 * An instance belongs to only one sid and won't be reused when switching sessions.
 *
 * Why not rely solely on `sessionId`: `useRoute()` returns a globally shared reactive route singleton,
 * referenced by **all** cached instances. When browser switches from sidA to sidB, `route.params.sid` globally becomes 'sidB',
 * so the `sessionId` computed of sidA instance (even if cached by KeepAlive and in inactive state) will also
 * recalculate to 'sidB'. Any event handlers depending on `sessionId` (like sid-based delete broadcast matching)
 * will misjudge. Hence we freeze sid into constant `mySid` when this instance is created, and use it for all
 * "which sid does this instance actually belong to" judgments, preventing cross-instance interference.
 */
const mySid = String(route.params.sid ?? '');

const todoStore = useTodoStore();
/** TaskFlow step progress for the floating panel (same init contract). */
const taskflowStore = useTaskflowStore();
const rightSidebarStore = useRightSidebarStore();
/** Mandatory new-session preset dialog (owned by the shell). */
const newSession = useNewSessionStore();
/** Per-session project-directory chip store (hydrated on mount + turn start). */
const projectDirectory = useProjectDirectoryStore();
/** Running tool calls mirror (terminal entry) and the access-mode shield. */
const runningCommands = useRunningCommandsStore();
const accessModeStore = useAccessModeStore();

/** The chat list, so the toolbar's terminal entry can reveal a command's card. */
const chatBoxRef = useTemplateRef<{ scrollToMessage: (id: number) => void; openFind: () => void }>('chatBoxRef');

/**
 * Reference to the input box component instance: its exposed draft/mention
 * surface is what edit mode and the selection/mention relays drive.
 */
const chatInputBoxRef = useTemplateRef<InstanceType<typeof ChatInputBox>>('chatInputBoxRef');

/** Hidden generic file input (the + menu's "add file" entry). */
const fileAttachmentInputRef = useTemplateRef<HTMLInputElement>('fileAttachmentInputRef');

// ── Shared page state (owned here, used by the slices below) ────────────────

/**
 * Message list to render for current session — single source of truth.
 *
 * Historically, direct overall assignment to `currentSession.value` (`= {...}`) caused
 * race condition where "late results from loadSessionHistory overwrite user's just-sent local messages",
 * manifesting as the list being cleared right after sending. Now all appending/merging operates only on this array; the session object is never reconstructed wholesale.
 */
const chatMessages = ref<MessageItem[]>([]);

/** Whether currently in AI reply generation */
const isSending = ref(false);

/** Current ongoing streaming request controller (used to stop generation) */
const activeAgentController = ref<ChatController | null>(null);

/**
 * Turn number currently receiving streamed chunks (a normal send or a HITL
 * resume). Shared by the chat-stream and HITL slices so every session-level
 * chunk of the ONE persistent socket lands on the right bubble.
 */
const streamingTurn = ref<number | null>(null);

/**
 * The session's ONE persistent agent WebSocket. Acquired once per page, it is
 * reused for every send / stop / HITL response, so replies can never be
 * misrouted between concurrent sends.
 */
const agentSocket = acquireAgentSocket(mySid);

// Background commands of this session: the http snapshot on mount / reconnect,
// the socket frames as deltas (see use-background-tasks). Command-level: the
// kill never touches the turn.
const {
  tasks: backgroundTasks,
  runningCount: backgroundTasksRunning,
  killing: backgroundTasksKilling,
  killRefusal: backgroundTasksKillRefusal,
  load: loadBackgroundTasks,
  kill: killBackgroundTask,
  onStarted: onBackgroundTaskStarted,
  onExit: onBackgroundTaskExit,
  onKillAck: onBackgroundTaskKillAck
} = useBackgroundTasks(() => mySid, agentSocket);

/**
 * Kill one background command and re-pull the snapshot once shortly after.
 *
 * The watcher's `terminal_bg_exit` is the row's authority, but it is a
 * best-effort WS push: a socket that reconnects around the kill can miss it, and
 * the row would keep a stop control that can never succeed again. The section
 * shows "stopping" for a requested stop either way; this re-pull and the one on
 * every popover open are what settle the final status from the snapshot.
 * @param taskId The backend's `bg-…` task id.
 */
const stopBackgroundTask = (taskId: string): void => {
  killBackgroundTask(taskId);
  window.setTimeout(() => void loadBackgroundTasks(), 1200);
};

/** Input box draft (controlled, two-way bound to inputBox.vue via defineModel) */
const draft = ref('');

// ── Slices: lifecycle / drafts / chunk rendering / HITL / streaming ────────

const { characterInfo, ensureSessionCharacter, loadSessionHistory } = useSessionLifecycle(chatMessages);

const drafts = useDraftPersistence(chatMessages);

// Scroll-up pagination: pulling to the top fetches the next older turn window
// and prepends it; the virtualizer's end anchoring keeps the viewport stable.
const {
  loadingOlder,
  loadOlder: loadOlderHistory,
  resetExhausted
} = useChatOlderHistory({
  sessionId: () => mySid,
  messages: () => chatMessages.value,
  prepend: rows => {
    const known = new Set(chatMessages.value.map(m => m.id));
    const fresh = rows.filter(m => !known.has(m.id));
    if (!fresh.length) return;
    // Rows are older than everything loaded (ascending), so a head-insert
    // preserves the list's ascending turn order.
    chatMessages.value = [...fresh, ...chatMessages.value];
  }
});

/**
 * The list released its oldest loaded rows to stay under its memory cap: drop
 * them from the in-memory history and re-arm paging, so scrolling back up
 * fetches them again (the Dexie cache and the server still have them).
 * @param messageIds Ids the list let go of.
 */
const onReleaseHead = (messageIds: number[]) => {
  if (!messageIds.length) return;
  const released = new Set(messageIds);
  chatMessages.value = chatMessages.value.filter(message => !released.has(message.id));
  resetExhausted();
};

// Toolbar-derived state: context ring usage, running-command mirror, shield
// refresh at turn boundaries and the terminal entry's "reveal this row".
const { lastPromptTokens, onTasksFocus } = useSessionToolbarState({
  chatMessages,
  isSending,
  sessionId,
  runningCommands,
  accessModeStore,
  openSubagentActivity: runId => rightSidebarStore.openTab('subagentActivity', { runId }),
  scrollToMessage: messageId => chatBoxRef.value?.scrollToMessage(messageId)
});

const chunks = useStreamChunks(chatMessages, drafts.allocateTempId, drafts);

const hitl = useHitlApproval({
  chatMessages,
  sessionId,
  isSending,
  activeAgentController,
  socket: agentSocket,
  streamingTurn,
  loadSessionHistory,
  drafts,
  chunks
});

const { hitlRequest, handleHitlDecision, restorePendingHitl } = hitl;

// Pending attachments (image / audio / video pickers + the whiteboard append).
const {
  selectedImages,
  selectedAudios,
  selectedVideos,
  triggerImagePicker,
  onImageSelected,
  removeImage,
  triggerAudioPicker,
  onAudioSelected,
  removeAudio,
  triggerVideoPicker,
  onVideoSelected,
  removeVideo,
  getPendingMedia,
  clearMediaSelection,
  addPendingImage
} = usePendingMedia();

// "Am I a branch child?" + branching from one answer (navigation stays here).
const { isForkChild, refreshBranchLink, forkAtTurn } = useSessionBranch({ mySid, isSending });

// Transcript selection: quote chip into the composer / ask in a side chat.
const { onSelectionAdd, onSelectionSideChat } = useSessionSelection({
  mySid,
  chatInput: () => chatInputBoxRef.value,
  openSideChat: request => rightSidebarStore.openTab('sideChat', { ...request, parentSessionId: mySid })
});

const stream = useChatStream({
  chatMessages,
  sessionId,
  mySid,
  draft,
  isSending,
  activeAgentController,
  socket: agentSocket,
  streamingTurn,
  t,
  getPendingMedia,
  clearMediaSelection,
  loadSessionHistory,
  drafts,
  chunks,
  hitl
});

const {
  handleSend,
  handleStop,
  handleContinueGeneration,
  handleEdit,
  handleSocketChunk,
  handleTurnStarted,
  handleQueued,
  handleSocketDone,
  reconnectState,
  queueBadgeList,
  cancelQueuedMessage,
  editQueuedMessage,
  sendNow,
  handleQueuedCancelled,
  handleQueuedUpdated,
  handleSendNowAck,
  clearQueueBadge,
  onStreamReconnecting,
  onStreamReconnected,
  onStreamReconnectFailed,
  onStreamResumed
} = stream;

// Composer edit mode: rewrite the newest sent user message and re-run that turn.
const { editingTurn, onEditMessage, cancelEditMessage, onComposerSend } = useSessionEditMode({
  draft,
  isSending,
  hitlRequest,
  chatInput: () => chatInputBoxRef.value,
  onSend: text => void handleSend(text),
  onEdit: (turnNum, text, rowId) => handleEdit(turnNum, text, rowId)
});

// The page installs the session-level socket handlers ONCE.
useSessionSocket({
  socket: agentSocket,
  handlers: {
    onChunk: handleSocketChunk,
    onQueued: handleQueued,
    onQueuedCancelled: handleQueuedCancelled,
    onQueuedUpdated: handleQueuedUpdated,
    onSendNowAck: handleSendNowAck,
    onTerminalBgStarted: onBackgroundTaskStarted,
    onTerminalBgExit: onBackgroundTaskExit,
    onTerminalBgKillAck: onBackgroundTaskKillAck,
    onDone: handleSocketDone
  },
  onHitl: hitl.handleHitlRequest,
  onTurnStarted: handleTurnStarted,
  afterTurnStarted: () => {
    // A directory choice parked mid-turn is promoted exactly at the turn
    // boundary, so the turn starting NOW may already run against the new root
    // while the chip still shows the old one — refresh it from the backend
    // (the promote path pushes no frame of its own).
    void projectDirectory.hydrate(mySid);
  }
});

// KeepAlive activation + history loading: first load, switch-away restore and
// the session-deleted stream abort.
const { isHistoryLoading, handleAbortStreamOnDelete } = useSessionActivation({
  mySid,
  sessionId,
  chatMessages,
  isSending,
  activeAgentController,
  draft,
  hitl,
  chatInput: () => chatInputBoxRef.value,
  clearQueueBadge,
  clearMediaSelection,
  ensureSessionCharacter,
  refreshBranchLink,
  syncHistory: sid => stream.syncHistory(sid),
  restorePendingHitl,
  initStores: sid => {
    // Pull the session plan snapshot and the TaskFlow board that feeds the
    // floating progress panel (idempotent singleton listeners + one refresh frame).
    todoStore.init(sid);
    taskflowStore.init(sid);
  },
  registerFindShortcut: handler => registerGlobalHandler('find-in-conversation', handler),
  unregisterFindShortcut: handler => unregisterGlobalHandler('find-in-conversation', handler),
  openFindInConversation: () => chatBoxRef.value?.openFind()
});

/**
 * Create session: the mandatory preset dialog (mounted in the shell) picks the
 * preset, applies it and creates the session — this entry point only opens it,
 * so the empty state and the toolbar command share one implementation.
 */
const handleCreateSession = () => {
  newSession.openDialog();
};

/**
 * Branch from one AI answer: create the child at that turn and SWITCH to it.
 *
 * Unlike a quote side chat (which opens a right-sidebar panel), a branch is a
 * session of its own — the page navigates there, carrying the parent's model,
 * thinking, directory and agent config (the server copies them).
 * @param turnNum The answered turn to fork at (1-based).
 */
async function onForkTurn(turnNum: number): Promise<void> {
  const childSid = await forkAtTurn(turnNum);
  if (!childSid) return;
  await router.push(localePath(`/home/${childSid}`));
}

/**
 * The queue row's text was edited in place: the row is the source of truth, so
 * the blur-time `editQueuedMessage` reads this value.
 * @param msgId The queued message's protocol id.
 * @param text The new text.
 */
const onQueueTextUpdate = (msgId: string, text: string): void => {
  const badge = queueBadgeList.value.find(item => item.msgId === msgId);
  if (badge) badge.text = text;
};

/**
 * A "add this file to the chat" request from the file tree (or a sidebar tab
 * drag relayed the same way): insert the mention chip when the request names
 * THIS instance's session — several sessions stay KeepAlive-cached, and the
 * event is app-wide.
 * @param request
 */
function onFileMentionRequest(request: unknown): void {
  const payload = request as { sessionId?: string; payload?: FileMentionPayload } | undefined;
  if (!payload?.payload?.path || payload.sessionId !== mySid) return;
  chatInputBoxRef.value?.addFileMention?.(payload.payload);
}

/**
 * The whiteboard's "add to chat": push the exported PNG into the pending image
 * tray.
 *
 * Mirrors the file-mention relay (the board panel is outside this page's
 * component tree). The tray is the ONE input the send path reads, so the image
 * gets the preview chip, the 10-image cap and the upload pipeline for free.
 * The `mySid` check is the frozen session id, so a KeepAlive-cached page of
 * another session never receives the drawing.
 * @param request `{ sessionId, payload: { base64, name } }` from the board.
 */
function onAddImageRequest(request: unknown): void {
  const payload = request as { sessionId?: string; payload?: { base64?: string; name?: string } } | undefined;
  const base64 = payload?.payload?.base64;
  if (!base64 || payload?.sessionId !== mySid) return;
  addPendingImage(base64, payload?.payload?.name || 'whiteboard.png');
}

/** Open the OS picker for the composer + menu's generic "add file" entry. */
function triggerFilePicker(): void {
  fileAttachmentInputRef.value?.click();
}

/**
 * Read one picked file as base64. A DataURL keeps the existing attachment
 * pipeline (the same `MediaPick` shape the three media pickers produce).
 * @param file The picked file.
 * @returns The file's DataURL (including the `data:` prefix).
 */
function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * Generic file attachments: any file the picker returns becomes a pending
 * attachment through the SAME tray the image/audio/video pickers use, so the
 * send path, the preview strip and the per-message cap are shared (the tray
 * caps at 10; a full list warns and stops the loop).
 * @param event The hidden input's change event.
 */
async function onFileAttachmentSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  // Snapshot before resetting: `input.files` is live and empties with `value`.
  const files = Array.from(input.files ?? []);
  input.value = ''; // allow picking the same file again
  for (const file of files) {
    const before = selectedImages.value.length;
    try {
      const dataUrl = await readFileAsDataUrl(file);
      addPendingImage(extractBase64(dataUrl), file.name);
    } catch (e) {
      logUtil.w('[sid] Failed to read a file attachment:', file.name, e);
      continue;
    }
    // The tray's cap refused this one and already told the user: stop.
    if (selectedImages.value.length === before) break;
  }
}

/**
 * A reconnect may have missed `terminal_bg_started` / `_exit` frames: re-pull the
 * snapshot so the strip matches the backend again.
 * @param payload Reconnect payload (`sessionId` optional).
 */
const onBackgroundTasksReconnected = (payload: unknown) => {
  const info = (payload ?? {}) as { sessionId?: string };
  if (!info.sessionId || info.sessionId === mySid) void loadBackgroundTasks();
};

// After mount, ensure character info is loaded (immediate already fired on KeepAlive restore; this is a fallback)
onMounted(() => {
  if (sessionId.value) {
    ensureSessionCharacter(sessionId.value);
    // A panel opened after a reconnect has missed every frame until now.
    void loadBackgroundTasks();
  }
  on('stream:reconnected', onBackgroundTasksReconnected);
  // Subscribe to the "session deleted → abort stream generation" event.
  // That session may be inactive yet still KeepAlive cached with an un-aborted stream; on deletion,
  // home/index.vue broadcasts, and this handler aborts this instance's AbortController.
  on(SESSION_ABORT_STREAM_EVENT, handleAbortStreamOnDelete);
  // Subscribe to "background tasks" show / chat switch events (broadcast by the sidebar)
  // Subscribe to WS stream reconnection events (broadcast by bridge.sendChatMessageWs, drives the reconnect banner)
  on('stream:reconnecting', onStreamReconnecting);
  on('stream:reconnected', onStreamReconnected);
  on('stream:reconnect:failed', onStreamReconnectFailed);
  on('stream:resumed', onStreamResumed);
  // The left sidebar's file tree has no reference to this composer, so its
  // "Add to chat" asks over the bus; only the instance owning that session
  // performs the insert.
  on('chat:file-mention', onFileMentionRequest);
  // The whiteboard panel hands its PNG over the same bus.
  on('chat:add-image', onAddImageRequest);
});

// Remove listeners on component unmount (KeepAlive cache slot evicted/destroyed) to avoid leaks
onUnmounted(() => {
  off(SESSION_ABORT_STREAM_EVENT, handleAbortStreamOnDelete);
  off('stream:reconnecting', onStreamReconnecting);
  off('stream:reconnected', onStreamReconnected);
  off('stream:reconnected', onBackgroundTasksReconnected);
  off('stream:reconnect:failed', onStreamReconnectFailed);
  off('stream:resumed', onStreamResumed);
  off('chat:file-mention', onFileMentionRequest);
  off('chat:add-image', onAddImageRequest);
  // Cancel this page's pending draft-write debounce timers (the slice lives in
  // this component's closure; an orphaned timer would fire into a dead page).
  drafts.dispose();
  // This instance's KeepAlive slot was evicted: release its persistent socket.
  releaseAgentSocket(mySid);
});
</script>

<style scoped>
/* WS stream reconnect banner fade in/out (paired with <Transition name="reconn-fade">) */
.reconn-fade-enter-active,
.reconn-fade-leave-active {
  transition: opacity 0.25s ease;
}
.reconn-fade-enter-from,
.reconn-fade-leave-to {
  opacity: 0;
}
</style>
