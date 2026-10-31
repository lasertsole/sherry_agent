<template>
  <div class="flex flex-col flex-1 h-full bg-transparent dark:bg-transparent">
    <!-- Chat main area / empty state (with a session sid shows the chat panel; the root path without sid shows "start a new chat").
        Background tasks live in the toolbar's terminal entry (the third icon's
        popover, TasksButton), so this column is always the chat. -->
    <div
      v-if="sessionId"
      class="flex flex-col flex-1 h-full min-h-0">
      <div class="flex flex-1 flex-col min-h-0">
        <ChatBox
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
            <div
              v-if="queueBadgeList.length > 0 && !hitlRequest"
              class="absolute top-0 left-0 right-0 z-20 bg-sky-600/90 text-white shadow-sm max-h-32 overflow-y-auto"
              role="status"
              aria-live="polite">
              <div
                v-for="badge in queueBadgeList"
                :key="badge.msgId"
                class="flex items-center gap-2 py-1.5 px-3 text-xs font-medium border-b border-white/10 last:border-b-0">
                <i
                  class="pi pi-clock"
                  aria-hidden="true"></i>
                <span class="shrink-0">
                  {{
                    t('chatInput.queuedPosition', {
                      position: badge.position,
                      queueSize: badge.queueSize
                    })
                  }}
                </span>
                <input
                  v-model="badge.text"
                  class="flex-1 min-w-0 rounded bg-white/10 px-1.5 py-0.5 outline-none focus:bg-white/20"
                  :aria-label="t('chatInput.editQueued')"
                  @keydown.enter="($event.target as HTMLInputElement).blur()"
                  @blur="editQueuedMessage(badge.msgId, badge.text)" />
                <button
                  class="shrink-0 rounded bg-white/20 px-2 py-0.5 font-medium transition-colors hover:bg-white/30"
                  @click="sendNow(badge.msgId)">
                  {{ t('chatInput.sendNow') }}
                </button>
                <button
                  class="shrink-0 rounded bg-red-500/40 px-1.5 py-0.5 transition-colors hover:bg-red-500/60"
                  :aria-label="t('chatInput.cancelQueued')"
                  @click="cancelQueuedMessage(badge.msgId)">
                  <i
                    class="pi pi-times"
                    style="font-size: 0.6rem"
                    aria-hidden="true"></i>
                </button>
              </div>
            </div>
          </Transition>
          <!-- Chat input box area (fixed h-40, keeping the send button position stable) -->
          <div class="flex flex-col h-40">
            <!-- Chat tools (hidden while a HITL request occupies the input slot).
                 [@container]: with both sidebars open this row narrows to ~280px
                 while its content needs ~472px, so below 500px only the three
                 anchors survive — the media (+) entry, the access-mode list and
                 the project-directory chip (right side); the task badge, the
                 context ring, the model picker and the thinking control hide.
                 `hidden!` is the important form: PrimeVue's own display rules and
                 the sibling `flex` utility are unlayered/later, so the plain
                 variant loses the tie. -->
            <div
              v-show="!hitlRequest"
              class="@container h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
              <!-- Media entry: image / audio / video uploads collapsed into one
                   dropdown (same trigger + popup shape as the model picker) -->
              <MediaMenu @select="event => handleOperate('toolBar', event)" />
              <!-- Access mode: one position right of the media entry, showing the
                   current mode's shield (confirm changes / auto-edit / full
                   access). -->
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
              <!-- Hidden image file input: triggered by the media menu's image entry via triggerImagePicker() -->
              <input
                ref="imageFileInputRef"
                type="file"
                accept="image/*"
                multiple
                class="hidden"
                @change="onImageSelected" />
              <!-- Hidden audio file input: triggered by the media menu's audio entry via triggerAudioPicker() -->
              <input
                ref="audioFileInputRef"
                type="file"
                accept="audio/*"
                multiple
                class="hidden"
                @change="onAudioSelected" />
              <!-- Hidden video file input: triggered by the media menu's video entry via triggerVideoPicker() -->
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
              <!-- exit_plan_mode: the plan for approval (approve / request changes) -->
              <template v-else-if="isPlanHitl">
                <div class="text-sm font-semibold">
                  {{ t('hitl.planTitle') }}
                  <span
                    v-if="planRevision"
                    class="text-xs text-gray-400 ml-2">
                    {{ planRevision }}
                  </span>
                </div>
                <pre
                  class="flex-1 min-h-0 overflow-auto text-xs whitespace-pre-wrap bg-gray-50 dark:bg-gray-800 p-2 rounded-lg m-0"
                  >{{ planText }}</pre>
                <div class="flex items-center gap-2">
                  <input
                    v-model="planFeedback"
                    type="text"
                    class="flex-1 px-3 py-1.5 text-sm rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-transparent outline-none focus:border-theme-main"
                    :placeholder="t('hitl.planFeedbackPlaceholder')"
                    @keydown.enter.prevent="revisePlan" />
                  <Button
                    :label="t('hitl.planRevise')"
                    size="small"
                    severity="warn"
                    variant="text"
                    :disabled="!planFeedback.trim()"
                    @click="revisePlan" />
                  <Button
                    :label="t('hitl.planApprove')"
                    icon="pi pi-check"
                    size="small"
                    @click="approvePlan" />
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
                    v-if="(hitlRequest?.allowed_decisions ?? []).includes('approve_dir')"
                    :label="t('hitl.approveDir')"
                    icon="pi pi-folder"
                    severity="secondary"
                    size="small"
                    :title="t('hitl.approveDirTooltip')"
                    @click="handleHitlDecision('approve_dir')" />
                  <Button
                    v-if="(hitlRequest?.allowed_decisions ?? []).includes('session')"
                    :label="t('hitl.approveSession')"
                    icon="pi pi-bookmark"
                    severity="secondary"
                    size="small"
                    :title="t('hitl.approveSessionTooltip')"
                    @click="handleHitlDecision('session')" />
                  <Button
                    :label="t('hitl.approve')"
                    icon="pi pi-check"
                    size="small"
                    @click="handleHitlDecision('approve')" />
                </div>
              </template>
            </div>
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
              @send="onComposerSend"
              @stop="handleStop" />
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
// Page-level error capture: runtime errors for all descendant components (ChatBox/HITL card/SubagentTasksView, etc.)
// → logUtil logs + global toast, return false prevents bubbling up to home/index.vue
// (03-errorCaptured factory function pattern)
useErrorCaptured();

// components
import ChatBox from '../components/ChatBox.vue';
import { ChatInputBox } from '#components';
// function
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import type { MessageItem } from '../type.ts';
import { CHAT_ROLE } from '@/types/chat-role';
import { buildSessionToolbarCommands } from '../session-toolbar';
import type { ChatController } from '@/composables/messages';
import type { FileMentionPayload } from '~/composables/use-file-mention';
import type { SelectionPayload } from '~/composables/use-chat-selection';
import { logUtil } from '~/utils/log';
// Bridge modules are not auto-imported (same explicit-import convention as the
// composables that call them).
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchSessionLink } from '~/composables/bridge/session';
import ThinkingToggle from '../components/ThinkingToggle.vue';
import ContextUsageButton from '../components/ContextUsageButton.vue';
import TasksButton from '../components/TasksButton.vue';
import AccessModePicker from '../components/AccessModePicker.vue';
import SessionModelPicker from '../components/SessionModelPicker.vue';
import ProjectDirectoryChip from '../components/ProjectDirectoryChip.vue';
import MediaMenu from '../components/MediaMenu.vue';
import { useNewSessionStore } from '~/stores/new-session';
import { useTaskflowStore } from '~/stores/taskflow';
import { useTodoStore } from '~/stores/todo';

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

const { focusRun } = useSubagentTasks();
const todoStore = useTodoStore();
/** TaskFlow step progress for the floating panel (same init contract). */
const taskflowStore = useTaskflowStore();
const rightSidebarStore = useRightSidebarStore();
/** Mandatory new-session preset dialog (owned by the shell). */
const newSession = useNewSessionStore();
/** Per-session project-directory chip store (hydrated on mount + turn start). */
const projectDirectory = useProjectDirectoryStore();

/**
 * Show this session's background tasks in the right sidebar: the chat column
 * stays in place (the view used to replace it) and the detail opens as a tab.
 * @param runId Optional run to locate inside the view.
 */
const openTaskDetail = (runId?: string) => {
  focusRun(runId);
  rightSidebarStore.openTab('taskDetail');
};

/** The chat list, so the toolbar's terminal entry can reveal a command's card. */
const chatBoxRef = useTemplateRef<{ scrollToMessage: (id: number) => void; openFind: () => void }>('chatBoxRef');

/**
 * A row of the toolbar's terminal entry was picked: show where that work lives.
 *
 * A sub-agent opens the background-task view focused on its run (the live
 * context of the child agent, the same destination as the sidebar's task
 * items); a terminal command reveals its own tool card in the chat, because the
 * card is where the command's output streams — no extra panel needed.
 * @param payload Which row was picked (`run` / `command`) and its id.
 * @param payload.kind
 * @param payload.id
 */
const onTasksFocus = (payload: { kind: 'run' | 'command'; id: string | number }) => {
  if (payload.kind === 'run') {
    openTaskDetail(String(payload.id));
    return;
  }
  // The command's log is in the chat: reveal its card in place.
  void nextTick(() => chatBoxRef.value?.scrollToMessage(Number(payload.id)));
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
  // Claim the find chord for THIS page: a cached sibling may have owned it.
  registerGlobalHandler('find-in-conversation', openFindBar);
  // When returning to this session, refresh the HITL card that may still be pending approval (idempotent: early-return if a card already exists or a resume is in flight)
  if (mySid) restorePendingHitl(mySid);
  // Pull the session plan snapshot (idempotent singleton listeners + one refresh frame).
  if (mySid) todoStore.init(mySid);
  // Same pull for the TaskFlow board that feeds the floating progress panel.
  if (mySid) taskflowStore.init(mySid);
});
onDeactivated(() => {
  isActive.value = false;
  // Release the chord only if it is still ours (a newer page keeps its own).
  unregisterGlobalHandler('find-in-conversation', openFindBar);
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

/**
 * Prompt size the provider reported for the newest finished turn — the context
 * the model actually saw. Drives the toolbar's usage ring before (and between)
 * the backend reads.
 */
const lastPromptTokens = computed(() => {
  for (let i = chatMessages.value.length - 1; i >= 0; i -= 1) {
    const message = chatMessages.value[i];
    if (message && message.role === CHAT_ROLE.AI && message.inputTokens) return message.inputTokens;
  }
  return 0;
});

/**
 * Running tool calls of this session feed the toolbar's terminal entry: a TOOL
 * row is only reported while the session is generating (a finished turn — or a
 * turn paused on an approval card — owns nothing that is executing).
 */
const runningCommands = useRunningCommandsStore();
watch([chatMessages, isSending], ([messages, sending]) => runningCommands.sync(messages, sending), {
  immediate: true,
  deep: false
});

/**
 * The shield's mode is DERIVED server-side — plan mode mirrors the plan status
 * — so a turn that enters or leaves plan mode changes it. Refresh at every
 * turn boundary: the same refresh covers a plan just approved (the status
 * flips to ready and the control falls back to the normal modes) and one the
 * agent entered by itself.
 */
const accessModeStore = useAccessModeStore();
watch(isSending, sending => {
  if (!sending && sessionId.value) accessModeStore.hydrate(sessionId.value);
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

// ── Plan-approval HITL view (exit_plan_mode: the plan + feedback) ──
const isPlanHitl = computed(() => hitlRequest.value?.tool_name === 'exit_plan_mode');
const planArgs = computed<Record<string, unknown>>(() => hitlRequest.value?.tool_args ?? {});
const planText = computed(() => {
  const p = planArgs.value['plan'];
  return typeof p === 'string' ? p : (hitlRequest.value?.description ?? '');
});
const planRevision = computed(() => {
  const r = planArgs.value['revision'];
  return typeof r === 'string' ? r : '';
});
const planFeedback = ref('');

/** Approve the plan: plan mode exits server-side and execution begins. */
const approvePlan = () => {
  planFeedback.value = '';
  handleHitlDecision('approve');
};

/** Send the plan back with the typed feedback (reject + message = revise it). */
const revisePlan = () => {
  const message = planFeedback.value.trim();
  if (!message) return;
  planFeedback.value = '';
  handleHitlDecision('reject', message);
};

// Reset the question-view state whenever a new HITL request arrives.
watch(hitlRequest, () => {
  customAnswer.value = '';
  selectedQuestionOptions.value = [];
  planFeedback.value = '';
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

/**
 * Edit mode: the turn being rewritten (`null` = the composer sends normally).
 * The row id travels with it so the backend truncates at the EXACT message
 * rather than guessing from the turn number.
 */
const editingTurn = ref<{ turnNum: number; rowId: number } | null>(null);

/**
 * Enter edit mode for a sent user message: prefill the composer and focus it.
 * @param message The clicked row.
 */
const onEditMessage = (message: MessageItem) => {
  if (isSending.value || hitlRequest.value) return;
  editingTurn.value = { turnNum: message.turn_num, rowId: message.id };
  chatInputBoxRef.value?.setDraftText?.(message.content);
};

/** Leave edit mode without touching the transcript. */
const cancelEditMessage = () => {
  editingTurn.value = null;
  draft.value = '';
  chatInputBoxRef.value?.setDraftText?.('');
};

/**
 * Composer submit: in edit mode the text rewrites that turn (truncate + re-run),
 * otherwise it is a normal send.
 * @param text The composer's text (chips already extracted).
 */
const onComposerSend = (text: string) => {
  const editing = editingTurn.value;
  if (!editing) {
    void handleSend(text);
    return;
  }
  editingTurn.value = null;
  handleEdit(editing.turnNum, text, editing.rowId);
};

// Esc leaves edit mode (never while a turn streams: the composer is read-only
// then, and its own Esc handling must stay untouched).
const onEditModeKeydown = (event: KeyboardEvent) => {
  if (!editingTurn.value || event.key !== 'Escape') return;
  cancelEditMessage();
};
/**
 * The conversation's find bar, as a GLOBAL shortcut action.
 *
 * The PAGE owns the action, not ChatBox: every KeepAlive-cached session keeps a
 * ChatBox mounted, so a per-instance listener would open a bar in a session the
 * reader cannot see. The handler is registered while this page is active (the
 * chord itself is rebindable in 菜单 → 系统设置) and checks `isActive` anyway.
 */
const openFindBar = (): void => {
  if (!isActive.value || !mySid) return;
  chatBoxRef.value?.openFind();
};

if (typeof window !== 'undefined') {
  window.addEventListener('keydown', onEditModeKeydown);
  onUnmounted(() => window.removeEventListener('keydown', onEditModeKeydown));
}

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

// The page installs the session-level socket handlers ONCE: every frame of the
// persistent session socket (chunk / hitl_request / turn_started / queued /
// queued_cancelled / queued_updated / send_now_ack / done) is routed here
// regardless of which send produced it.
agentSocket.setHandlers({
  onChunk: handleSocketChunk,
  onHitl: hitl.handleHitlRequest,
  onTurnStarted: info => {
    handleTurnStarted(info);
    // A directory choice parked mid-turn is promoted exactly at the turn
    // boundary, so the turn starting NOW may already run against the new root
    // while the chip still shows the old one — refresh it from the backend
    // (the promote path pushes no frame of its own).
    void projectDirectory.hydrate(mySid);
  },
  onQueued: handleQueued,
  onQueuedCancelled: handleQueuedCancelled,
  onQueuedUpdated: handleQueuedUpdated,
  onSendNowAck: handleSendNowAck,
  onTerminalBgStarted: onBackgroundTaskStarted,
  onTerminalBgExit: onBackgroundTaskExit,
  onTerminalBgKillAck: onBackgroundTaskKillAck,
  onDone: handleSocketDone
});

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
 * Session toolbar command registry (former 4-case switch over the event).
 * `createSession` stays a registry entry even though the empty-state button
 * invokes `handleCreateSession` directly — the dispatch surface is unchanged.
 */
const sessionToolbarCommands = buildSessionToolbarCommands({
  createSession: () => handleCreateSession(),
  uploadImage: () => triggerImagePicker(),
  uploadAudio: () => triggerAudioPicker(),
  uploadVideo: () => triggerVideoPicker()
});

/**
 * Tool trigger
 * @param type
 * @param event
 */
const handleOperate = (type: string, event: string) => {
  if (!event || !type) return;
  sessionToolbarCommands[event]?.();
};

/**
 * Create session: the mandatory preset dialog (mounted in the shell) picks the
 * preset, applies it and creates the session — this entry point only opens it,
 * so the empty state and the toolbar command share one implementation.
 */
const handleCreateSession = () => {
  newSession.openDialog();
};

/**
 * Whether THIS session is itself a fork (a side chat or a branch).
 *
 * The server refuses to fork a fork (no nested side chats), so the AI rows hide
 * the branch action here instead of offering an action that must fail. The link
 * is fetched once per session load; an unreachable server reads as "root".
 *
 * Declared BEFORE `doLoadFor`: the session watch below is `immediate`, so
 * setup reaches `refreshBranchLink` before the rest of the setup body runs.
 */
const isForkChild = ref(false);

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
  // "Am I a branch child?" — one lookup per session load (fail-open; a root
  // session reads as false and keeps the fork action visible).
  void refreshBranchLink(sid);
  // One round trip: history + the running turn's snapshot. Frames arriving
  // during the request are held and flushed after the snapshot is applied.
  void stream.syncHistory(sid);
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
  const MAX_PENDING_IMAGES = 10;
  if (selectedImages.value.length >= MAX_PENDING_IMAGES) {
    toastWarn(t('chatInput.maxImagesExceed', { count: MAX_PENDING_IMAGES, extra: 1 }));
    return;
  }
  selectedImages.value = [...selectedImages.value, { base64, name: payload?.payload?.name || 'whiteboard.png' }];
}

/** True while a side session is being created (guards a double-click). */
const creatingSideChat = ref(false);

/**
 * The localized "from X" tooltip for a quote chip.
 * @param role The quoted message's role.
 */
function selectionRoleLabel(role?: string): string {
  if (role === CHAT_ROLE.USER) return t('chat.selection.sourceUser');
  if (role === CHAT_ROLE.AI) return t('chat.selection.sourceAi');
  return t('chat.selection.sourceTool');
}

/**
 * "添加到当前会话" (add to this conversation): insert the selected text as a quote chip at the caret.
 * @param payload The quoted selection.
 */
function onSelectionAdd(payload: SelectionPayload): void {
  if (!payload?.text) return;
  chatInputBoxRef.value?.addQuoteChip?.(payload.text, selectionRoleLabel(payload.sourceRole));
}

/**
 * "在辅助会话中提问" (ask in a side session): create a normal session derived from this one (same
 * preset binding and character snapshot, no subagent semantics), open it in a
 * right-sidebar side-chat tab and hand it the quoted text.
 *
 * The child is an ephemeral session: it inherits a model-only snapshot of this
 * session's history, is hidden from the normal session list, and is purged
 * when this tab closes (or this parent session is deleted).
 * @param payload The quoted selection.
 */
/**
 * Refresh the branch flag for a session (fail-open: unknown reads as root).
 * @param sid
 */
async function refreshBranchLink(sid: string): Promise<void> {
  isForkChild.value = false;
  if (!sid) return;
  try {
    isForkChild.value = (await fetchSessionLink(sid)) !== null;
  } catch (e) {
    logUtil.w('[Branch] parent link lookup failed:', e);
  }
}

/**
 * Branch from one AI answer: create the child at that turn and SWITCH to it.
 *
 * Unlike a quote side chat (which opens a right-sidebar panel), a branch is a
 * session of its own — the page navigates there, carrying the parent's model,
 * thinking, directory and agent config (the server copies them).
 * @param turnNum The answered turn to fork at (1-based).
 */
async function onForkTurn(turnNum: number): Promise<void> {
  if (isSending.value || isForkChild.value || !mySid || turnNum <= 0) return;
  try {
    const { sessionId: childSid } = await createSideChatSession(mySid, '', turnNum);
    await router.push(localePath(`/home/${childSid}`));
  } catch (e) {
    logUtil.e('[Branch] Failed to create the branch:', e);
    toastError(t('errors.forkFailed'), e instanceof Error ? e.message : String(e));
  }
}

async function onSelectionSideChat(payload: SelectionPayload): Promise<void> {
  if (!payload?.text || creatingSideChat.value || !mySid) return;
  creatingSideChat.value = true;
  try {
    const { sessionId: childSid, inherited } = await createSideChatSession(mySid, t('chat.selection.sideChatTitle'));
    rightSidebarStore.openTab('sideChat', {
      sessionId: childSid,
      parentSessionId: mySid,
      inherited,
      quote: payload.text,
      source: selectionRoleLabel(payload.sourceRole)
    });
  } catch (e) {
    logUtil.e('[SideChat] Failed to create the side session:', e);
    toastError(t('chat.selection.sideChatFailed'), e instanceof Error ? e.message : String(e));
  } finally {
    creatingSideChat.value = false;
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

<i18n lang="json">
{
  "zh": {
    "hitl": {
      "approve": "批准",
      "approveDir": "批准整个目录",
      "approveDirTooltip": "允许整个目录（本会话内有效，子代理同样适用），目录内文件不再弹出审批",
      "approveSession": "本会话允许",
      "approveSessionTooltip": "允许本会话内执行相同命令，不再弹出审批（前两个词匹配，如 \"git push\" 覆盖所有 git push 子命令）",
      "reject": "拒绝",
      "title": "操作需要审批",
      "tool": "工具",
      "yolo": "同意所有操作",
      "yoloTooltip": "同意本次及本会话后续所有操作，不再弹出审批",
      "customPlaceholder": "输入自定义回答…",
      "submit": "提交",
      "planTitle": "计划待审批",
      "planApprove": "批准计划",
      "planRevise": "退回修改",
      "planFeedbackPlaceholder": "指出需要修改的地方…"
    }
  },
  "en": {
    "hitl": {
      "approve": "Approve",
      "approveDir": "Approve Entire Directory",
      "approveDirTooltip": "Allow the entire directory for this session (also inherited by subagents) — files inside never prompt again",
      "approveSession": "Allow for Session",
      "approveSessionTooltip": "Allow the same command for this session — no more prompts (matches the first two words, e.g. \"git push\" covers all git push subcommands)",
      "reject": "Reject",
      "title": "Action Requires Approval",
      "tool": "Tool",
      "yolo": "Approve All (YOLO)",
      "yoloTooltip": "Approve this and all future actions in this session — no more approval prompts",
      "customPlaceholder": "Type a custom answer…",
      "submit": "Submit",
      "planTitle": "Plan Awaiting Approval",
      "planApprove": "Approve Plan",
      "planRevise": "Request Changes",
      "planFeedbackPlaceholder": "What should change? …"
    }
  },
  "ja": {
    "hitl": {
      "approve": "承認",
      "approveDir": "ディレクトリ全体を承認",
      "approveDirTooltip": "ディレクトリ全体を許可します（このセッション内有効、サブエージェントにも継承）。配下のファイルでは確認ダイアログは表示されません",
      "approveSession": "セッション内で許可",
      "approveSessionTooltip": "このセッション内で同じコマンドを許可し、確認を省略（先頭2語でマッチ、\"git push\" はすべての git push サブコマンドをカバー）",
      "reject": "拒否",
      "title": "操作の承認が必要です",
      "tool": "ツール",
      "yolo": "すべての操作を承認",
      "yoloTooltip": "今回とこのセッションの以後の操作をすべて承認し、確認ダイアログは表示されません",
      "customPlaceholder": "カスタム回答を入力…",
      "submit": "送信",
      "planTitle": "計画の承認待ち",
      "planApprove": "計画を承認",
      "planRevise": "修正を依頼",
      "planFeedbackPlaceholder": "修正すべき点を入力…"
    }
  },
  "ko": {
    "hitl": {
      "approve": "승인",
      "approveDir": "디렉터리 전체 승인",
      "approveDirTooltip": "디렉터리 전체를 허용합니다(이 세션에 한정, 하위 에이전트에도 상속). 디렉터리 내 파일은 다시 승인을 요구하지 않습니다",
      "approveSession": "세션 내 허용",
      "approveSessionTooltip": "이 세션 내에서 동일한 명령을 허용하고 확인을 생략 (처음 두 단어로 매칭, \"git push\"는 모든 git push 하위 명령을 포함)",
      "reject": "거부",
      "title": "작업 승인이 필요합니다",
      "tool": "도구",
      "yolo": "모든 작업 승인",
      "yoloTooltip": "이번 작업과 이 세션의 이후 모든 작업을 승인하며 확인 창이 다시 표시되지 않습니다",
      "customPlaceholder": "직접 답변 입력…",
      "submit": "제출",
      "planTitle": "계획 승인 대기",
      "planApprove": "계획 승인",
      "planRevise": "수정 요청",
      "planFeedbackPlaceholder": "수정할 점을 입력하세요…"
    }
  }
}
</i18n>
