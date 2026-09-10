<template>
  <div class="flex flex-col flex-1 h-full bg-transparent dark:bg-transparent">
    <!-- Chat main area / empty state (with a session sid, shows the chat panel or the background task list page; only the root path without sid shows "start a new chat").
        The chat area and the background task view stay **permanently mounted** while sid exists, toggled only via v-show,
        so clicking a background task within the same session only switches focus (focusRun) — SubagentTasksView is not remounted,
        initTasks/HTTP refetching is not triggered, and the G6 graph is not rebuilt. -->
    <div
      v-if="sessionId"
      class="flex flex-col flex-1 h-full min-h-0">
      <div
        v-show="viewMode === 'chat'"
        class="flex-1 flex flex-col min-h-0">
        <!-- "View Background Tasks" jump bar: shown only when the current session has background tasks (running/finished).
            Clicking navigates to the standalone tasks page /home/tasks/{sid} (rather than the right-side viewMode='tasks' embedded view),
            making it easy to inspect this session's full task execution chain on a large viewport. -->
        <div
          v-if="taskRuns.length > 0"
          class="shrink-0 mx-2 mt-2 flex items-center gap-2 bg-white dark:bg-[#131619] rounded-lg border border-solid border-gray-light dark:border-gray-dark shadow-sm px-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-[#1a1d21] transition-colors select-none"
          role="button"
          tabindex="0"
          @click="router.push(localePath(`/home/tasks/${sessionId}`))"
          @keydown.enter.prevent="router.push(localePath(`/home/tasks/${sessionId}`))"
          @keydown.space.prevent="router.push(localePath(`/home/tasks/${sessionId}`))">
          <i class="pi pi-sitemap text-sm text-theme-main"></i>
          <span class="flex-1 text-sm font-medium text-gray-900 dark:text-gray-100">
            {{ t('taskViewer.viewTasks') }}
          </span>
          <i class="pi pi-angle-right text-xs text-gray-400"></i>
        </div>
        <ChatBox
          :messages="chatMessages"
          :user-avatar="characterInfo.userAvatar"
          :ai-avatar="characterInfo.aiAvatar"
          :user-name="characterInfo.userName"
          :ai-name="characterInfo.aiName" />
        <!-- Image preview area (kept separate above the input box, so it does not squeeze the h-40 input box pushing the send button up / clipping the ✕ button) -->
        <template v-if="selectedImages.length > 0">
          <div
            class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
            <div
              v-for="(img, idx) in selectedImages"
              :key="idx"
              class="relative shrink-0 group">
              <img
                :src="`data:image/*;base64,${img.base64}`"
                :alt="img.name"
                class="w-16 h-16 object-cover rounded-lg border border-solid border-gray-light dark:border-gray-dark cursor-pointer hover:opacity-80 transition-opacity duration-200"
                role="button"
                tabindex="0"
                :aria-label="t('a11y.previewImage')"
                @click="openPreview(`data:image/*;base64,${img.base64}`)"
                @keydown.enter.prevent="openPreview(`data:image/*;base64,${img.base64}`)"
                @keydown.space.prevent="openPreview(`data:image/*;base64,${img.base64}`)" />
              <button
                type="button"
                :title="t('chatBox.removeImage')"
                class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
                @click="removeImage(idx)">
                ✕
              </button>
            </div>
          </div>
        </template>
        <!-- Audio preview area (kept separate above the input box, at the same level as the image preview area) -->
        <template v-if="selectedAudios.length > 0">
          <div
            class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
            <div
              v-for="(audio, idx) in selectedAudios"
              :key="idx"
              class="relative shrink-0 group">
              <div
                class="flex items-center gap-2 px-3 py-2 rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-gray-800">
                <span class="pi pi-volume-down text-xs text-[#6B7280]"></span>
                <span class="text-xs font-medium text-[#111827] dark:text-[#E5E7EB] max-w-32 truncate">{{
                  audio.name
                }}</span>
              </div>
              <button
                type="button"
                :title="t('chatBox.removeAudio')"
                class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
                @click="removeAudio(idx)">
                ✕
              </button>
            </div>
          </div>
        </template>
        <!-- Video preview area (kept separate above the input box, at the same level as the image/audio preview areas) -->
        <template v-if="selectedVideos.length > 0">
          <div
            class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
            <div
              v-for="(video, idx) in selectedVideos"
              :key="idx"
              class="relative shrink-0 group">
              <video
                :src="`data:video/*;base64,${video.base64}`"
                class="w-32 h-20 object-cover rounded-lg border border-solid border-gray-light dark:border-gray-dark"
                muted
                playsinline
                preload="metadata" />
              <button
                type="button"
                :title="t('chatBox.removeVideo')"
                class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
                @click="removeVideo(idx)">
                ✕
              </button>
            </div>
          </div>
        </template>
        <TodoDock />
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
          <!-- Queue badge: shown while the backend has enqueued this send (session busy; browser WS mode);
              follows the reconnect-banner pattern, disappears when the queued turn starts streaming or ends -->
          <Transition name="reconn-fade">
            <div
              v-if="queueBadge"
              class="absolute top-0 left-0 right-0 z-20 flex items-center justify-center gap-2 py-1.5 px-3 text-xs font-medium bg-sky-600/90 text-white shadow-sm"
              role="status"
              aria-live="polite">
              <i
                class="pi pi-clock"
                aria-hidden="true"></i>
              <span>{{
                t('chatInput.queued', { position: queueBadge.position, queueSize: queueBadge.queueSize })
              }}</span>
            </div>
          </Transition>
          <!-- Chat input box area (fixed h-40, keeping the send button position stable) -->
          <div class="flex flex-col h-40">
            <!-- Chat tools (hidden while a HITL request occupies the input slot) -->
            <div
              v-show="!hitlRequest"
              class="h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
              <div class="hidden sm:block">
                <Button
                  v-for="tool in tools"
                  :key="tool.event"
                  :icon="tool.icon"
                  :label="t(tool.toolName)"
                  @click="handleOperate('toolBar', tool.event)"
                  size="small"
                  variant="text" />
              </div>
              <div class="block sm:hidden">
                <Button
                  v-for="tool in tools"
                  :key="tool.event"
                  :icon="tool.icon"
                  :aria-label="t(tool.toolName)"
                  @click="handleOperate('toolBar', tool.event)"
                  size="small"
                  variant="text" />
              </div>
              <!-- Hidden image file input: triggered by the toolbar image button via triggerImagePicker() -->
              <input
                ref="imageFileInputRef"
                type="file"
                accept="image/*"
                multiple
                class="hidden"
                @change="onImageSelected" />
              <!-- Hidden audio file input: triggered by the toolbar audio button via triggerAudioPicker() -->
              <input
                ref="audioFileInputRef"
                type="file"
                accept="audio/*"
                multiple
                class="hidden"
                @change="onAudioSelected" />
              <!-- Hidden video file input: triggered by the toolbar video button via triggerVideoPicker() -->
              <input
                ref="videoFileInputRef"
                type="file"
                accept="video/*"
                multiple
                class="hidden"
                @change="onVideoSelected" />
            </div>
            <!-- HITL panel occupies the same slot as the input box; the two are mutually exclusive (v-show) -->
            <div
              v-show="hitlRequest"
              class="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col gap-2">
              <!-- question tool: selectable options + a trailing custom-input row -->
              <template v-if="isQuestionHitl">
                <div class="text-sm font-semibold">{{ questionHeader || t('hitl.title') }}</div>
                <div
                  v-if="questionText"
                  class="text-sm whitespace-pre-wrap text-gray-700 dark:text-gray-200">
                  {{ questionText }}
                </div>
                <div class="flex flex-col gap-1.5">
                  <button
                    v-for="(option, idx) in questionOptions"
                    :key="idx"
                    type="button"
                    class="text-left px-3 py-1.5 rounded-lg border border-solid transition-colors cursor-pointer"
                    :class="
                      isMultipleQuestion && selectedQuestionOptions.includes(option.label)
                        ? 'border-theme-main bg-theme-main/10'
                        : 'border-gray-light dark:border-gray-dark hover:bg-gray-50 dark:hover:bg-[#1a1d21]'
                    "
                    @click="selectQuestionOption(option.label)">
                    <span class="text-sm font-medium">{{ option.label }}</span>
                    <span
                      v-if="option.description"
                      class="text-xs text-gray-400 ml-2">
                      {{ option.description }}
                    </span>
                  </button>
                  <!-- trailing custom-input row -->
                  <div class="flex items-center gap-2">
                    <input
                      v-model="customAnswer"
                      type="text"
                      class="flex-1 px-3 py-1.5 text-sm rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-transparent outline-none focus:border-theme-main"
                      :placeholder="t('hitl.customPlaceholder')"
                      @keydown.enter.prevent="submitCustomAnswer" />
                    <Button
                      :label="t('hitl.submit')"
                      size="small"
                      :disabled="!customAnswer.trim() && selectedQuestionOptions.length === 0"
                      @click="submitCustomAnswer" />
                    <Button
                      :label="t('hitl.reject')"
                      size="small"
                      severity="danger"
                      variant="text"
                      @click="handleHitlDecision('reject')" />
                  </div>
                </div>
              </template>
              <!-- other tools: generic approval card -->
              <template v-else>
                <div class="text-sm font-semibold">{{ t('hitl.title') }}</div>
                <div class="text-sm text-gray-500">
                  {{ t('hitl.tool') }}: <span class="font-bold">{{ hitlRequest?.tool_name }}</span>
                </div>
                <div
                  v-if="hitlRequest?.description"
                  class="text-sm whitespace-pre-wrap">
                  {{ hitlRequest.description }}
                </div>
                <div
                  v-if="hitlRequest?.tool_args && Object.keys(hitlRequest.tool_args).length > 0"
                  class="text-xs bg-gray-50 dark:bg-gray-800 p-2 rounded-lg overflow-auto max-h-24">
                  <pre class="m-0">{{ JSON.stringify(hitlRequest.tool_args, null, 2) }}</pre>
                </div>
                <div class="mt-auto flex gap-2 justify-end">
                  <Button
                    :label="t('hitl.reject')"
                    icon="pi pi-times"
                    severity="danger"
                    size="small"
                    @click="handleHitlDecision('reject')" />
                  <Button
                    :label="t('hitl.yolo')"
                    icon="pi pi-bolt"
                    severity="warn"
                    size="small"
                    :title="t('hitl.yoloTooltip')"
                    @click="handleHitlDecision('yolo')" />
                  <Button
                    :label="t('hitl.approve')"
                    icon="pi pi-check"
                    size="small"
                    @click="handleHitlDecision('approve')" />
                </div>
              </template>
            </div>
            <!-- Input box: hidden while a HITL request occupies the same slot -->
            <ChatInputBox
              v-show="!hitlRequest"
              ref="chatInputBoxRef"
              v-model:draft="draft"
              :sending="isSending"
              :disabled="!!hitlRequest"
              :disabled-text="t('chatInput.waitingApproval')"
              @send="handleSend"
              @stop="handleStop" />
          </div>
        </div>
      </div>
      <!-- Background task list page: permanently mounted while sid exists (visibility toggled via v-show, no remount/refetch);
        clicking within the same session only switches focus via focusRun and highlights the root graph node in place. -->
      <SubagentTasksView
        v-show="viewMode === 'tasks'"
        :initial-run-id="targetRunId" />
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
// Page-level error capture: runtime errors for all descendant components (ChatBox/HITL card/SubagentTasksView, etc.)
// → logUtil logs + global toast, return false prevents bubbling up to home/index.vue
// (03-errorCaptured factory function pattern)
useErrorCaptured();

// components
import ChatBox from '../components/ChatBox.vue';
import TodoDock from '@/components/chat/TodoDock.vue';
import { ChatInputBox } from '#components';
// function
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import type { MessageItem } from '../type.ts';
import { tools } from '../config';
import type { ChatController } from '@/composables/messages';
import SubagentTasksView from '../components/SubagentTasksView.vue';

// Image preview
const { openPreview } = useImagePreview();

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

/**
 * Right-side display mode: 'chat' (chat area) | 'tasks' (background task list page).
 * This is a regular ref for this instance, only controls right-side area rendering content, doesn't affect KeepAlive cache / page-key mechanism.
 */
const viewMode = ref<'chat' | 'tasks'>('chat');
/** run_id carried when clicking sidebar task items, used for locating/expanding/highlighting that run in the task list page. */
const targetRunId = ref<string | undefined>(undefined);

const { taskRuns, initTasks, setTasksTabActive } = useSubagentTasks();
const { init: initTodoList } = useTodoList();

/**
 * Receive 'show background tasks' event: switch to task list page and record the run_id to locate (if any).
 * @param payload
 */
// mitt Handler<unknown> requires the (event: unknown) signature; narrow the broadcast value manually
// (sidebar emits a string run_id or undefined).
const onShowTasks = (payload: unknown) => {
  const runId = typeof payload === 'string' ? payload : undefined;
  targetRunId.value = runId;
  viewMode.value = 'tasks';
};

/** Receive 'show chat' event: restore chat area and sync sidebar tab state. */
const onShowChat = () => {
  viewMode.value = 'chat';
  setTasksTabActive(false);
};

/**
 * Whether this instance is in 'active' state (false when hidden in KeepAlive cache).
 * KeepAlive cache **does not pause** reactive watch/effects of cached instances — when switching to sidB,
 * global `route` changes in all inactive instances will still trigger their `watch(sessionId)`.
 * Use this flag to distinguish "whether this instance is currently being displayed", combined with `mySidLoaded` to implement:
 *  - Switch away (inactive) → preserve memory state, never execute destructive clearing;
 *  - Switch back (reactivated) → restore as-is if already loaded (drafts/scroll/streaming/HITL), no reloading.
 */
const isActive = ref(false);
onActivated(() => {
  isActive.value = true;
  // When returning to this session, refresh the HITL card that may still be pending approval (idempotent: early-return if a card already exists or a resume is in flight)
  if (mySid) restorePendingHitl(mySid);
  // Prefetch background tasks for this session (idempotent: only actually fetch when session switches or list is empty),
  // Used by "View Background Tasks" jump bar to determine whether to show (don't show if no tasks).
  if (mySid) initTasks(mySid);
  // Pull the session plan snapshot (idempotent singleton listeners + one refresh frame).
  if (mySid) initTodoList(mySid);
});
onDeactivated(() => {
  isActive.value = false;
});

/**
 * Whether this instance has already loaded history for 'its own session (mySid)'.
 * The first KeepAlive cached instance only loads history on **first** mount; when switching back to this session later
 * (sessionId changes back from other sid to mySid) it just restores memory state as-is, no repeated clearing/loading,
 * thus preserving unpersisted drafts, scroll position, and messages still streaming in background.
 */
let mySidLoaded = false;

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

/** Input box draft (controlled, two-way bound to inputBox.vue via defineModel) */
const draft = ref('');

// ── Slices: lifecycle / drafts / chunk rendering / HITL / streaming ────────

const { characterInfo, ensureSessionCharacter, loadSessionHistory } = useSessionLifecycle(chatMessages);

const drafts = useDraftPersistence(chatMessages);

const chunks = useStreamChunks(chatMessages, drafts.allocateTempId, drafts);

const hitl = useHitlApproval({
  chatMessages,
  sessionId,
  isSending,
  activeAgentController,
  loadSessionHistory,
  drafts,
  chunks
});

const { hitlRequest, handleHitlDecision, restorePendingHitl } = hitl;

// ── Question-tool HITL view (selectable options + custom answer) ──
interface QuestionOptionView {
  label: string;
  description?: string;
}

const questionArgs = computed<Record<string, unknown>>(() => hitlRequest.value?.tool_args ?? {});
const isQuestionHitl = computed(() => hitlRequest.value?.tool_name === 'question');
const questionText = computed(() => {
  const q = questionArgs.value['question'];
  return typeof q === 'string' ? q : (hitlRequest.value?.description ?? '');
});
const questionHeader = computed(() => {
  const h = questionArgs.value['header'];
  return typeof h === 'string' ? h : '';
});
const questionOptions = computed<QuestionOptionView[]>(() => {
  const raw = questionArgs.value['options'];
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((o): o is Record<string, unknown> => typeof o === 'object' && o !== null)
    .map(o => ({
      label: String(o['label'] ?? ''),
      description: typeof o['description'] === 'string' ? o['description'] : undefined
    }))
    .filter(o => o.label.length > 0);
});
const isMultipleQuestion = computed(() => questionArgs.value['multiple'] === true);
const customAnswer = ref('');
const selectedQuestionOptions = ref<string[]>([]);

/**
 * Send the chosen answer (option label(s) and/or custom text) back through the HITL resume.
 * @param answer
 */
const submitQuestionAnswer = (answer: string) => {
  customAnswer.value = '';
  selectedQuestionOptions.value = [];
  handleHitlDecision('approve', answer);
};

/**
 * Single-select sends immediately; multi-select toggles the option in the selection.
 * @param label
 */
const selectQuestionOption = (label: string) => {
  if (isMultipleQuestion.value) {
    const i = selectedQuestionOptions.value.indexOf(label);
    if (i >= 0) selectedQuestionOptions.value.splice(i, 1);
    else selectedQuestionOptions.value.push(label);
  } else {
    submitQuestionAnswer(label);
  }
};

/** Submit the custom answer, combined with any selected options in multi-select mode. */
const submitCustomAnswer = () => {
  const parts = isMultipleQuestion.value ? [...selectedQuestionOptions.value] : [];
  const text = customAnswer.value.trim();
  if (text) parts.push(text);
  if (parts.length) submitQuestionAnswer(parts.join(', '));
};

// Reset the question-view state whenever a new HITL request arrives.
watch(hitlRequest, () => {
  customAnswer.value = '';
  selectedQuestionOptions.value = [];
});

// ── Media selection (per-kind picker slice; template owns the hidden inputs) ──

const {
  selected: selectedImages,
  trigger: triggerImagePicker,
  onSelected: onImageSelected,
  remove: removeImage
} = useMediaPicker('image');

const {
  selected: selectedAudios,
  trigger: triggerAudioPicker,
  onSelected: onAudioSelected,
  remove: removeAudio
} = useMediaPicker('audio');

const {
  selected: selectedVideos,
  trigger: triggerVideoPicker,
  onSelected: onVideoSelected,
  remove: removeVideo
} = useMediaPicker('video');

/** Snapshot the pending media base64 payloads for the outgoing message. */
const getPendingMedia = () => ({
  images: selectedImages.value.map(img => img.base64),
  audios: selectedAudios.value.map(a => a.base64),
  videos: selectedVideos.value.map(v => v.base64)
});

/** Clear the pending media selections (send took them). */
const clearMediaSelection = () => {
  selectedImages.value = [];
  selectedAudios.value = [];
  selectedVideos.value = [];
};

const stream = useChatStream({
  chatMessages,
  sessionId,
  mySid,
  draft,
  isSending,
  activeAgentController,
  t,
  getPendingMedia,
  clearMediaSelection,
  setTasksTabActive,
  loadSessionHistory,
  drafts,
  chunks,
  hitl
});

const {
  handleSend,
  handleStop,
  reconnectState,
  queueBadge,
  clearQueueBadge,
  onStreamReconnecting,
  onStreamReconnected,
  onStreamReconnectFailed
} = stream;

/**
 * Reference to the input box component instance: when the session is deleted (frontend broadcasts `SESSION_ABORT_STREAM_EVENT`),
 * call its `clearHistory()` to wipe the history cache left in this session's KeepAlive cache slot,
 * so history is strictly cleared along with the session deletion (even if that slot has not been LRU-evicted yet).
 */
const chatInputBoxRef = useTemplateRef<InstanceType<typeof ChatInputBox>>('chatInputBoxRef');

/**
 * Stream abort handling when session is deleted:
 *
 * When this session is deleted (home/index.vue broadcasts `SESSION_ABORT_STREAM_EVENT`),
 * if this instance is exactly that session (matched by sid) and still streaming, abort its AbortController.
 * Particularly crucial for 'inactive but KeepAlive cached and stream not aborted' sessions — if not aborted after deletion,
 * backend will continue pushing chunks to deleted session's WebSocket, causing deleted chat state to be contaminated.
 *
 * Note: the handler must be defined in this scope,
 * and compare the first parameter (session id) with this instance `sessionId` to ensure only this session is aborted.
 * @param deletedSid
 */
const handleAbortStreamOnDelete = (deletedSid: unknown) => {
  // Use frozen this instance `mySid` for comparison, not live `sessionId`: the latter reads global route,
  // when instance is KeepAlive cached (switched to other session) it becomes others' sid, causing this session deletion to miss comparison、
  // background stream cannot be aborted.
  if (deletedSid !== mySid) return;
  if (activeAgentController.value) {
    activeAgentController.value.abort();
    activeAgentController.value = null;
    isSending.value = false;
  }
  // Session gone: drop the queue badge so it cannot linger on a deleted session's cached instance
  clearQueueBadge();
  // Session deleted: clear remaining history cache/draft browsing state in this instance's KeepAlive cache slot
  // (when deleting inactive session slot may not be released immediately, history residing in slot must be actively cleared,
  //   ensuring history strictly follows session deletion, avoiding manually revisiting that sid to see deleted session residues).
  chatInputBoxRef.value?.clearHistory?.();
  // Session deleted: clear all ongoing draft turns for this session in IndexedDB, prevent orphan drafts
  // from incorrectly re-hydrating after rebuilding same id session (Draft table still contains original deleted session content).
  void clearDraftSession(mySid);
};

/**
 * Tool trigger
 * @param type
 * @param event
 */
const handleOperate = (type: string, event: string) => {
  if (!event || !type) return;
  // Toolbar
  switch (event) {
    case 'createSession':
      handleCreateSession();
      return;
    case 'uploadImage':
      triggerImagePicker();
      return;
    case 'uploadAudio':
      triggerAudioPicker();
      return;
    case 'uploadVideo':
      triggerVideoPicker();
      return;
    default:
      return;
  }
};

/** Create session: generate a random session_id, create a new session window and switch to it */
const handleCreateSession = () => {
  const newSessionId = crypto.randomUUID();
  router.push({ name: 'home-sid', params: { sid: newSessionId } });
  // New session: immediately create and lock a character snapshot from the current global profile, ensuring avatar/name display correctly
  ensureSessionCharacter(newSessionId);
  // Persist the placeholder session (same behavior as home/index.vue) so a new session created from the toolbar/empty state survives a refresh
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  cacheSessionMeta({ id: newSessionId, title: t('history.newSession'), createTime, updatedAt: Date.now() });
};

/**
 * Load history for the specified session for this instance (clears local state, then rebuilds). Only called on new sessions / exception fallback paths.
 * @param sid
 */
const doLoadFor = (sid: string) => {
  // When switching to / first-loading a new sid, clear all session-scoped local state first, then load this session's own history.
  // If chatMessages still holds the previous session's messages, even a loadSessionHistory dedup merge by id
  // would mix old and new messages together, causing "switched to the new session but the old session's content is displayed".
  chatMessages.value = [];
  isSending.value = false;
  // Discard the in-flight request controller left over from the previous session (its stream was invalidated by the session switch / is no longer usable)
  activeAgentController.value = null;
  hitl.abortResume();
  // The previous session's pending-approval card / input draft / selected images must not leak into the new session
  hitl.clearRequest();
  draft.value = '';
  clearMediaSelection();
  // Load this session's locked character snapshot (if none, lock using the global profile)
  ensureSessionCharacter(sid);
  loadSessionHistory(sid);
  // Restore a possibly still-pending HITL interrupt card in three-tier persistence scenarios (session switch / refresh / browser reopen / server restart)
  restorePendingHitl(sid);
  mySidLoaded = true;
};

// On first screen, load the current session's history messages and render the merged list into ChatBox
watch(
  sessionId,
  sid => {
    if (!sid) return;

    // 1) First time (history not yet loaded for mySid): only load this instance's own session history.
    //    This `[sid].vue` instance froze sid into mySid at creation time and belongs to that one session only,
    //    so the first load only loads mySid — never any other session's content.
    //    Note: with `immediate: true`, the watcher fires synchronously during setup when isActive is still false
    //    (onActivated has not run yet), so the first-load check cannot rely on isActive and must fall back to mySidLoaded.
    if (!mySidLoaded) {
      doLoadFor(mySid);
      return;
    }

    // 2) This instance is KeepAlive cached and inactive (the user has switched to another session):
    //    `route.params.sid` (the global route singleton) has become someone else's value, so this instance's sessionId recomputes to the other id.
    //    But this instance's own in-memory state (chatMessages/drafts/scroll/streaming activeAgentController/HITL card)
    //    must be preserved as-is, to be restored when switching back — early-return here, never perform destructive clearing.
    //    Otherwise, switching away and back would wipe all conversation/streaming state (dialog gone / background stream aborted).
    if (!isActive.value) return;

    // 3) Active, already loaded, and switched back to this very session (sessionId changed back to mySid):
    //    restore the in-memory state as-is, no repeated clear+load, naturally preserving messages still streaming
    //    in the background, drafts, and scroll position.
    //    — Hardening: if the in-memory state was polluted (it contains another session's messages, e.g. wrongly written by (4) before)
    //    or is empty, reload this session's history to guarantee the switch-back always displays this session's own content. Otherwise just an idempotent refresh.
    if (sid === mySid) {
      const polluted = chatMessages.value.some(m => m.session_id && m.session_id !== mySid);
      if (chatMessages.value.length === 0 || polluted) {
        doLoadFor(mySid);
      } else {
        ensureSessionCharacter(mySid);
        restorePendingHitl(mySid);
      }
      return;
    }

    // 4) Active but sessionId is not this session: this is usually the "moment of switching away", where the watcher fires
    //    before onDeactivated (route already changed to the other sid, isActive not yet false).
    //    Never treat it as a new session and call `doLoadFor(sid)` — that would write another session's messages into
    //    this instance's memory, and on switch-back (3) would display the wrong session's content. Here the local state
    //    must be preserved as-is until onDeactivated sets isActive=false; on switch-back, (3) handles restore/fallback reload.
    return;
  },
  { immediate: true }
);

// After mount, ensure character info is loaded (immediate already fired on KeepAlive restore; this is a fallback)
onMounted(() => {
  if (sessionId.value) {
    ensureSessionCharacter(sessionId.value);
  }
  // Subscribe to the "session deleted → abort stream generation" event.
  // That session may be inactive yet still KeepAlive cached with an un-aborted stream; on deletion,
  // home/index.vue broadcasts, and this handler aborts this instance's AbortController.
  on(SESSION_ABORT_STREAM_EVENT, handleAbortStreamOnDelete);
  // Subscribe to "background tasks" show / chat switch events (broadcast by the sidebar)
  on('subagent:show-tasks', onShowTasks);
  on('subagent:show-chat', onShowChat);
  // Subscribe to WS stream reconnection events (broadcast by bridge.sendChatMessageWs, drives the reconnect banner)
  on('stream:reconnecting', onStreamReconnecting);
  on('stream:reconnected', onStreamReconnected);
  on('stream:reconnect:failed', onStreamReconnectFailed);
});

// Remove listeners on component unmount (KeepAlive cache slot evicted/destroyed) to avoid leaks
onUnmounted(() => {
  off(SESSION_ABORT_STREAM_EVENT, handleAbortStreamOnDelete);
  off('subagent:show-tasks', onShowTasks);
  off('subagent:show-chat', onShowChat);
  off('stream:reconnecting', onStreamReconnecting);
  off('stream:reconnected', onStreamReconnected);
  off('stream:reconnect:failed', onStreamReconnectFailed);
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

<i18n lang="json">
{
  "zh": {
    "chatInput": {
      "queued": "已排队 · 第 {position} 位（共 {queueSize} 个）",
      "waitingApproval": "等待审批..."
    },
    "chatBox": {
      "removeImage": "移除图片",
      "removeAudio": "移除音频",
      "removeVideo": "移除视频"
    },
    "connection": {
      "reconnecting": "连接中断，正在重连…（第 {attempt}/{max} 次）"
    },
    "hitl": {
      "approve": "批准",
      "reject": "拒绝",
      "title": "操作需要审批",
      "tool": "工具",
      "yolo": "同意所有操作",
      "yoloTooltip": "同意本次及本会话后续所有操作，不再弹出审批",
      "customPlaceholder": "输入自定义回答…",
      "submit": "提交"
    },
    "taskViewer": {
      "viewTasks": "查看后台任务"
    }
  },
  "en": {
    "chatInput": {
      "queued": "Queued · position {position} of {queueSize}",
      "waitingApproval": "Waiting for approval..."
    },
    "chatBox": {
      "removeImage": "Remove image",
      "removeAudio": "Remove audio",
      "removeVideo": "Remove video"
    },
    "connection": {
      "reconnecting": "Connection lost, reconnecting… (attempt {attempt}/{max})"
    },
    "hitl": {
      "approve": "Approve",
      "reject": "Reject",
      "title": "Action Requires Approval",
      "tool": "Tool",
      "yolo": "Approve All (YOLO)",
      "yoloTooltip": "Approve this and all future actions in this session — no more approval prompts",
      "customPlaceholder": "Type a custom answer…",
      "submit": "Submit"
    },
    "taskViewer": {
      "viewTasks": "View Background Tasks"
    }
  },
  "ja": {
    "chatInput": {
      "queued": "順番待ち · {queueSize} 件中 {position} 番目",
      "waitingApproval": "承認待ち..."
    },
    "chatBox": {
      "removeImage": "画像を削除",
      "removeAudio": "音声を削除",
      "removeVideo": "動画を削除"
    },
    "connection": {
      "reconnecting": "接続が切断されました。再接続中…（{attempt}/{max} 回目）"
    },
    "hitl": {
      "approve": "承認",
      "reject": "拒否",
      "title": "操作の承認が必要です",
      "tool": "ツール",
      "yolo": "すべての操作を承認",
      "yoloTooltip": "今回とこのセッションの以後の操作をすべて承認し、確認ダイアログは表示されません",
      "customPlaceholder": "カスタム回答を入力…",
      "submit": "送信"
    },
    "taskViewer": {
      "viewTasks": "バックグラウンドタスクを表示"
    }
  },
  "ko": {
    "chatInput": {
      "queued": "대기 중 · {queueSize}개 중 {position}번째",
      "waitingApproval": "승인 대기 중..."
    },
    "chatBox": {
      "removeImage": "이미지 제거",
      "removeAudio": "오디오 제거",
      "removeVideo": "비디오 제거"
    },
    "connection": {
      "reconnecting": "연결 끊김, 재연결 중… ({attempt}/{max}번째 시도)"
    },
    "hitl": {
      "approve": "승인",
      "reject": "거부",
      "title": "작업 승인이 필요합니다",
      "tool": "도구",
      "yolo": "모든 작업 승인",
      "yoloTooltip": "이번 작업과 이 세션의 이후 모든 작업을 승인하며 확인 창이 다시 표시되지 않습니다",
      "customPlaceholder": "직접 답변 입력…",
      "submit": "제출"
    },
    "taskViewer": {
      "viewTasks": "백그라운드 작업 보기"
    }
  }
}
</i18n>
