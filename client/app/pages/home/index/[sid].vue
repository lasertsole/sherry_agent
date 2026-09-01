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
        <!-- HITL approval card: non-modal, placed at the top of the history message area (does not cover the latest conversation),
            occupying its own slot so it never blocks viewing other messages; it can grow naturally with its content. -->
        <div
          v-if="hitlRequest"
          class="shrink-0 mx-2 mt-2 bg-white dark:bg-[#131619] rounded-lg border border-solid border-gray-light dark:border-gray-dark shadow-lg">
          <div class="flex flex-col gap-3 p-3">
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
              class="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded-lg overflow-auto max-h-40">
              <pre class="m-0">{{ JSON.stringify(hitlRequest.tool_args, null, 2) }}</pre>
            </div>
            <div class="flex gap-2 justify-end">
              <Button
                :label="t('hitl.reject')"
                icon="pi pi-times"
                severity="danger"
                @click="handleHitlDecision('reject')" />
              <Button
                :label="t('hitl.approve')"
                icon="pi pi-check"
                @click="handleHitlDecision('approve')" />
            </div>
          </div>
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
          <!-- Chat input box area (fixed h-40, keeping the send button position stable) -->
          <div class="flex flex-col h-40">
            <!-- Chat tools -->
            <div class="h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
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
            <!-- Input box: input/send disabled while a HITL request is pending approval, with a waiting-for-approval hint -->
            <ChatInputBox
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
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();

// components
import ChatBox from '../components/ChatBox.vue';
import { ChatInputBox } from '#components';
// function
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import type { MessageItem, HitlRequestData } from '../type.ts';
import type { MultiModalMessage } from '@/types/message';
import { CHAT_ROLE } from '../type.ts';
import { toMessageItems } from '../messageItems';
import type { CachedCharacter } from '@/composables/db';
import {
  DEFAULT_CACHED_CHARACTER,
  cacheCharacter,
  readCachedCharacter,
  cacheSessionMeta,
  saveDraftTurn,
  readDraftTurns,
  clearDraftTurn,
  clearDraftSession
} from '@/composables/db';
import { tools } from '../config';
import { resumeHitl, StreamInterruptedError, type AgentChunkType } from '@/composables/bridge';
import {
  get_history_by_turn_page,
  getPendingInterrupt,
  postAgentStream,
  SESSION_ABORT_STREAM_EVENT
} from '@/composables/messages';
import { on, off } from '@/composables/mitt';
import { useSubagentTasks } from '@/composables/useSubagentTasks';
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

/** Receive 'show background tasks' event: switch to task list page and record the run_id to locate (if any). */
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

/**
 * Character display information (source is local Dexie session cache snapshot, see `CachedCharacter` in `db.ts`).
 * - `userAvatar` / `aiAvatar` are base64 data URL (user custom) or `/avatar/xxx.jpg` relative URL (built-in default), both can be directly rendered by `<img>`.
 * - Refreshed from corresponding session snapshot (or global pending profile) on each session switch/new creation, old sessions retain their own snapshots.
 */
const characterInfo = ref<{ userName: string; userAvatar: string; aiName: string; aiAvatar: string }>({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

/** Default character display info (built-in: Touno Hanna / Sherry Orange + default avatar URLs, see `defaultCharacter.ts`) */
const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

// ── Chat area background image (uniformly rendered by home/index.vue root container) ────────
// Background image is global configuration, bound to root container of home/index.vue (fills entire window, including left session list),
// Updated immediately by shared singleton useChatBackground after saving, no need to load/render background image in this page.
// This page root div is already set to bg-transparent (light theme) in template, allowing root container's background image to show through.

/**
 * Map a character snapshot to `characterInfo` (empty segments fall back to built-in defaults).
 */
const applyCharacterSnapshot = (snap?: Pick<CachedCharacter, 'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'>) => {
  const defaultInfo = defaultCharacter();
  characterInfo.value = snap
    ? {
        userName: snap.userName?.trim() ? snap.userName : defaultInfo.userName,
        userAvatar: snap.userAvatar ?? defaultInfo.userAvatar,
        aiName: snap.aiName?.trim() ? snap.aiName : defaultInfo.aiName,
        aiAvatar: snap.aiAvatar ?? defaultInfo.aiAvatar
      }
    : defaultInfo;
};

/**
 * Ensure the specified session has locked its own character snapshot and update `characterInfo` to that session's display info.
 *
 * Naming logic: System configuration - character configuration edits the 'global pending profile' (`GLOBAL_SESSION_KEY` row).
 * When each session is first opened, copy and lock the current global profile to its own `session_id` row;
 * Subsequent global updates (avatar/name changes) no longer affect old sessions with locked snapshots, only new sessions get the latest global values.
 *
 * @param sessionId Session ID
 */
const ensureSessionCharacter = async (sessionId: string) => {
  try {
    const [globalSnap, sessionSnap] = await Promise.all([
      readCachedCharacter('__global__'),
      readCachedCharacter(sessionId)
    ]);
    // Session already has snapshot (old session locked avatar/name) → use snapshot directly, not affected by global changes.
    if (sessionSnap) {
      applyCharacterSnapshot(sessionSnap);
      return;
    }
    // Session has no snapshot yet (new session or never opened before) → use global profile snapshot and lock it.
    // Note: `base` might be the global row (with session_id=GLOBAL_SESSION_KEY),
    // must use `...base` then explicitly override session_id, avoid writing real session key into global row.
    const base = globalSnap ?? defaultCharacter();
    const locked: CachedCharacter = { ...base, session_id: sessionId };
    await cacheCharacter(locked);
    applyCharacterSnapshot(locked);
  } catch (error) {
    // On Dexie read/write exceptions, preserve current display and don't block chat.
    console.warn('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
};

/**
 * Message list to render for current session — single source of truth.
 *
 * Historically, direct overall assignment to `currentSession.value` (`= {...}`) caused
 * race condition where "late results from loadSessionHistory overwrite user's just-sent local messages",
 * manifesting as the list being cleared right after sending. Now all appending/merging operates only on this array; the session object is never reconstructed wholesale.
 */
const chatMessages = ref<MessageItem[]>([]);

/**
 * Load history messages for specified session (local cache first, backend merges server-side increments),
 * merged into `chatMessages` (deduplicated by id), for ChatBox rendering.
 *
 * Fix: No longer reconstruct entire `currentSession.value` (that would overwrite user's already-sent local messages,
 * causing 'list cleared after sending'). Only merge history rows into single list, existing messages preserved.
 */
const loadSessionHistory = async (sessionId: string) => {
  const rows = await get_history_by_turn_page(sessionId, 0, 10, 1);
  const historyItems = toMessageItems(rows);

  // Merge and deduplicate: existing ids preserve local versions (including unsent temporary messages with negative ids),
  // server real ids are added as-is. Overall turn_num ascending ensures stable order.
  //
  // Race condition fix: unsent temporary messages have negative ids (handleSend assigns large negative),
  // when server later returns the real positive id row for the same message, their ids differ, deduplication by id would preserve both
  // 'temporary negative id copy' and 'server positive id row', causing the same message to render twice.
  //
  // Therefore for each local negative id temporary copy, directly match its real positive id in server history rows by
  // 'same session + same turn_num + same role + same content' exact match;
  // if hit, replace with server row (discard temporary copy). Note cannot only merge find by (session, turn, role)
  // —— multiple same-role rows may appear in same turn (e.g. tool call + final reply within one AI round,
  // add_messages writes the whole batch into same turn_num), merge keys would lose some rows. Line-by-line exact match
  // on content ensures no cross-row mistaken replacement.
  const mergedById = new Map<number, MessageItem>();
  const serverRowFor = (m: MessageItem) =>
    historyItems.find(
      h =>
        h.id >= 0 &&
        h.session_id === m.session_id &&
        h.turn_num === m.turn_num &&
        h.role === m.role &&
        h.content === m.content
    );
  for (const m of chatMessages.value) {
    // Local temporary negative id rows: if server has already returned positive id row for same logical message, skip (use server row).
    if (m.id < 0) {
      const serverRow = serverRowFor(m);
      // Hit: replace temporary copy with server positive id row, add in subsequent loop; placeholder here to avoid duplication
      if (serverRow) {
        mergedById.set(serverRow.id, serverRow);
        continue;
      }
    }
    mergedById.set(m.id, m);
  }
  for (const h of historyItems) {
    // Only add when local doesn't have message with same id, to avoid overwriting content already updated during streaming
    if (!mergedById.has(h.id)) mergedById.set(h.id, h);
  }

  // —— Draft Hydration ——
  // Read incomplete draft turns for this session in IndexedDB (turns not persisted due to error/stop/HITL-reject, etc.,
  // and turns currently being streamed generated when server hasn't yet written back onDone results).
  //
  // Each draft message preserves its 'local negative temporary id' and 'positive turn_num'. Since positive turn_num matches real-time messages,
  // draft rows naturally appear after committed messages in same turn (see sorting at end), won't float before committed turns like old negative turn scheme.
  // And exact match with server/local collection by (session, turn, role, content) — if same logical message is already persisted (serverRowFor hit)
  // or already exists in local collection, skip this draft row to avoid duplicate rendering of drafts and real-time messages in same turn.
  const drafts = await readDraftTurns(sessionId);
  //
  // Draft 'Stale Turn' Judgment: If same turn_num already has 'server-persisted positive id row' in merged collection,
  // and contains a final AI result not in streaming (role=ai) — then this turn has been successfully persisted by server,
  // this draft is stale residue from when the turn was interrupted (e.g. Test: manually kill backend to let draft retain 'reply failed' marker,
  // then backend self-heals and writes back real content for same turn). At this point, exact match by (turn, role, content) line by line
  // would be missed due to different content (failed marker vs real reply), causing failed marker/failed tool rows and server real rows
  // to render together, duplicating the same turn. Correct approach: Any draft turn whose final AI result has been persisted should be skipped entirely, no longer hydrated.
  //
  // Only using server role=ai behavior as 'persisted final result' anchor is because normal ongoing streaming turns server
  // only writes human first (positive id), AI result not yet persisted, at this point draft AI rows should still be hydrated; only when AI is persisted
  // does it mean the turn is substantially complete and the draft must be stale.
  const staleDraftTurns = new Set<number>();
  for (const m of mergedById.values()) {
    if (m.id >= 0 && m.role === 'ai') staleDraftTurns.add(m.turn_num);
  }
  for (const draft of drafts) {
    for (const dm of draft.messages) {
      if (dm.session_id !== sessionId) continue; // Defensive: only hydrate this session
      // Stale turn: this turn has been persisted with final AI result by server, discard draft row entirely
      if (staleDraftTurns.has(dm.turn_num)) continue;
      // Whether draft row already exists in local collection / server history (matched by logical keys)
      const alreadyLocal = [...mergedById.values()].some(
        m => m.turn_num === dm.turn_num && m.role === dm.role && m.content === dm.content
      );
      if (alreadyLocal) continue;
      mergedById.set(dm.id, dm);
    }
  }

  // Sort by turn_num ascending; within same turn, sort by id ascending (matches backend messages table
  // "ORDER BY turn_num ASC, id ASC"). Previously using id descending would reverse insertion order within same turn
  // (user message + AI reply share same turn_num), causing after refresh
  // AI replies to appear above user messages, last AI reply not at bottom.
  //
  // Draft rows use same positive turn_num + negative temporary id as real-time messages: for committed turns, draft id is negative,
  // real message id for that turn is positive, within same turn id ascending (negative < positive) drafts come first —— but same logical message
  // has been filtered out by 'skip' logic above, drafts that can be hydrated are all failed turns not yet persisted, thus won't
  // conflict with actual rendering.
  chatMessages.value = [...mergedById.values()].sort((a, b) => a.turn_num - b.turn_num || a.id - b.id);
};

/** Whether currently in AI reply generation */
const isSending = ref(false);
/** Current ongoing streaming request controller (used to stop generation) */
let activeAgentController: AbortController | null = null;

/**
 * WS stream reconnection status banner: null = not reconnecting; otherwise show 'Reconnecting (attempt/max times)'.
 * Data source is mitt events broadcast by bridge's sendChatMessageWs during exponential backoff reconnection
 * (stream:reconnecting / stream:reconnected / stream:reconnect:failed).
 */
const reconnectState = ref<{ attempt: number; max: number } | null>(null);

/** Reconnection events only drive this session's banner (route may cache multiple session instances simultaneously) */
// mitt Handler<unknown> requires the (event: unknown) signature; narrow the broadcast payload manually
// (bridge.sendChatMessageWs emits { sessionId?, attempt?, maxAttempts? }).
const onStreamReconnecting = (event: unknown) => {
  const payload = (typeof event === 'object' && event !== null ? event : {}) as {
    sessionId?: string;
    attempt?: number;
    maxAttempts?: number;
  };
  const current = sessionId.value || 'default';
  if (payload?.sessionId && payload.sessionId !== current) return;
  reconnectState.value = { attempt: payload?.attempt ?? 1, max: payload?.maxAttempts ?? 3 };
};
const onStreamReconnected = () => {
  reconnectState.value = null;
};
const onStreamReconnectFailed = () => {
  reconnectState.value = null;
};

/**
 * Post-interrupt delayed reconciliation: Backend only persists this round's messages when agent graph completes,
 * server may still be generating at interruption moment. Wait 25 seconds then pull history (loadSessionHistory has built-in positive/negative id deduplication),
 * replace local negative temporary id rows with server positive turn_num records, recover content generated before interruption.
 * Only execute if still on same session and not sending at that time; repeated interruptions reset timer (one-time semantics).
 */
let postInterruptTimer: ReturnType<typeof setTimeout> | null = null;
const schedulePostInterruptReconcile = (sid: string) => {
  if (postInterruptTimer) clearTimeout(postInterruptTimer);
  postInterruptTimer = setTimeout(() => {
    postInterruptTimer = null;
    if (sessionId.value === sid && !isSending.value) {
      void loadSessionHistory(sid);
    }
  }, 25_000);
};
/**
 * Auto-increment id counter (for local temporary messages, avoid conflict with real ids).
 *
 * Start from a large negative number and allocate in 'incrementing' order by creation time: -1000000, -999999, -999998 …
 * This way messages within same turn (turn_num same) when sorted by id ascending,
 * exactly equals their creation order (user message first, AI/tool segments follow),
 * maintaining consistency with backend "ORDER BY turn_num ASC, id ASC" (user written first, smaller id).
 *
 * Note: Cannot use `--tempIdCounter` (decrement) like before, otherwise later created AI/tool
 * message ids would be smaller, when switching away during streaming and back triggers re-sorting, AI would appear above user.
 */
let tempIdCounter = -1000000;

/**
 * Stream abort handling when session is deleted:
 *
 * When this session is deleted (home/index.vue broadcasts `SESSION_ABORT_STREAM_EVENT`),
 * if this instance is exactly that session (matched by sid) and still streaming, abort its AbortController.
 * Particularly crucial for 'inactive but KeepAlive cached and stream not aborted' sessions — if not aborted after deletion,
 * backend will continue pushing chunks to deleted session's WebSocket, causing deleted chat state to be contaminated.
 *
 * Note: `activeAgentController` is a setup closure variable, so handler must be defined in this scope,
 * and compare the first parameter (session id) with this instance `sessionId` to ensure only this session is aborted.
 */
const handleAbortStreamOnDelete = (deletedSid: unknown) => {
  // Use frozen this instance `mySid` for comparison, not live `sessionId`: the latter reads global route,
  // when instance is KeepAlive cached (switched to other session) it becomes others' sid, causing this session deletion to miss comparison、
  // background stream cannot be aborted.
  if (deletedSid !== mySid) return;
  if (activeAgentController) {
    activeAgentController.abort();
    activeAgentController = null;
    isSending.value = false;
  }
  // Session deleted: clear remaining history cache/draft browsing state in this instance's KeepAlive cache slot
  // (when deleting inactive session slot may not be released immediately, history residing in slot must be actively cleared,
  //   ensuring history strictly follows session deletion, avoiding manually revisiting that sid to see deleted session residues).
  chatInputBoxRef.value?.clearHistory?.();
  // Session deleted: clear all ongoing draft turns for this session in IndexedDB, prevent orphan drafts
  // from incorrectly re-hydrating after rebuilding same id session (Draft table still contains original deleted session content).
  void clearDraftSession(mySid);
};

/** HITL approval request (set when agent pauses waiting for human approval) */
const hitlRequest = ref<HitlRequestData | null>(null);

/** Handle HITL approval request: show approval dialog */
const handleHitlRequest = (data: HitlRequestData) => {
  hitlRequest.value = data;
};

/** Ongoing HITL resume controller (single-flight: only one allowed per session) */
let activeHitlController: { closed: boolean; abort: () => void } | null = null;

/**
 * User approve/reject HITL request.
 *
 * Decision no longer depends on `sendHitlResponse` mounted on the closure returned by `streamChatMessage` during real-time message sending —
 * that closure is only available when `!done && socket.readyState === OPEN`,
 * after page refresh/session switch/browser reopen socket is closed、controller is null, approval will silently no-op.
 * Here changed to independent `resumeHitl`: directly open a new WS to backend `/sessions/agent/ws`,
 * send `hitl_response` frame to streamingly restore agent from LangGraph checkpoint, thus
 * supporting three-layer persistence (session switch, refresh, browser reopen) and still being able to complete approval.
 */
const handleHitlDecision = (decision: 'approve' | 'reject', message: string = '') => {
  const sid = sessionId.value;
  if (!sid) {
    hitlRequest.value = null;
    return;
  }
  // single-flight only used to prevent duplicate submission for 'same pending approval item', absolutely cannot silently discard new decisions.
  // For sequential HITL (multiple dangerous tools requiring approval one by one), the previous resume WS is still in
  // streaming recovery (closed=false), if directly return at this point will cause subsequent 'approve/reject' clicks to have no response at all.
  // Correct approach: first abort/release the still-running controller slot, then open a new resume WS for this decision —
  // ensuring every click has a real channel to send hitl_response, absolutely no silent no-op.
  if (activeHitlController && !activeHitlController.closed) {
    // Abort old link's stream recovery (its abort will send {type:'stop'} to backend), and release its slot,
    // avoid it mistakenly clearing the already replaced activeHitlController once it resolves later.
    activeHitlController.abort();
    activeHitlController = null;
  }

  // Record the turn number for this approval: new messages from resume will go to 'current max turn + 1'
  const turnNum = chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  // Register this resume turn as draft (consistent with handleSend), so appendStreamChunk can write to disk in real-time;
  // remove during reconciliation when resume stream completes normally, retain draft to cache failure stage content on reject/failure.
  trackDraftTurn(sid, turnNum);

  const onChunk = (
    content: string,
    type: AgentChunkType,
    _sessionId: string,
    meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
  ) => {
    appendStreamChunk(sid, content, type, turnNum, meta);
  };

  const { controller, promise } = resumeHitl(sid, decision, message, onChunk, handleHitlRequest);
  activeHitlController = controller;

  // Reject: this tool won't be executed, backend won't send back tool_end, so mark the currently still running
  // tool card as failed (UI changes from spinner to red ✗), avoid permanent loading state.
  if (decision === 'reject') {
    markRunningToolsFailed();
    // Reject won't trigger backend response, immediately write draft with failed status to disk, ensuring failed progress is visible after refresh
    void writeDraftTurn(sid, turnNum);
  }

  /**
   * Clean up the hanging state of this HITL approval chain.
   *
   * Key point: When HITL interrupt occurs, backend **does not close** the original generation stream's WebSocket (waiting for resume),
   * so the promise returned by `postAgentStream` in `handleSend` hangs permanently, its `onDone` never triggers,
   * `isSending` stays at `true`. Must manually reset after approval completes, otherwise input box/generate button will be permanently locked.
   */
  const finish = () => {
    if (activeHitlController === controller) activeHitlController = null;
    // The original generation stream is abandoned: release its controller slot and reset the sending state
    activeAgentController = null;
    isSending.value = false;
    // If no new hitl_request is triggered during approval, close the approval card
    if (hitlRequest.value) {
      hitlRequest.value = null;
    }
  };
  promise
    .then(() => {
      // Normal completion: write final draft first then reconcile remove (consistent with handleSend onDone)
      return commitDraftTurn(sid, turnNum).then(() => {
        untrackDraftTurn(sid, turnNum);
        finish();
        void loadSessionHistory(sid);
      });
    })
    .catch(() => {
      // Also clean up on error, keep input available; card closing is decided by other processes
      if (activeHitlController === controller) activeHitlController = null;
      activeAgentController = null;
      // HITL resume failed: ongoing tools did not complete normally, marked as failed (red ✗)
      markRunningToolsFailed();
      // Retain draft: cache the completed stages before failure
      void writeDraftTurn(sid, turnNum);
      isSending.value = false;
    });

  // This approval has been answered; collapse the card (it pops up again if the agent pauses once more during the resume)
  hitlRequest.value = null;
};

/**
 * Try to restore the HITL interrupt card that is still "pending approval".
 *
 * In the three-tier persistence scenarios (session switch / page refresh / browser reopen / server restart),
 * `hitlRequest` only lives in component memory and is empty when re-entering the session. Here we query the backend
 * `/get_pending_interrupt` (re-pushed from the LangGraph checkpoint) for whether this session still has a pending
 * approval; if it does, the card is popped up again for the user to approve/reject.
 */
const restorePendingHitl = async (sid: string) => {
  if (!sid) return;
  // Do not re-raise if an approval is already in flight or a card already exists
  if (hitlRequest.value || (activeHitlController && !activeHitlController.closed)) return;
  const pending = await getPendingInterrupt(sid);
  if (pending && typeof pending === 'object' && !Array.isArray(pending) && typeof pending.tool_name === 'string') {
    hitlRequest.value = {
      tool_name: pending.tool_name,
      tool_args: pending.tool_args ?? {},
      description: pending.description ?? '',
      allowed_decisions: pending.allowed_decisions ?? []
    };
  }
};

/** Stop the current AI reply generation (local frontend abort + notify the backend to stop) */
const handleStop = () => {
  activeAgentController?.abort();
  activeAgentController = null;
  // If a HITL resume stream recovery is in flight, abort that controller as well
  // (its abort sends {type:'stop'} to the backend, making answering=False and triggering a CancelledError)
  activeHitlController?.abort();
  activeHitlController = null;
  // The aborted turn did not finish; mark the still-running tool cards as failed (red ✗)
  markRunningToolsFailed();
  // Aborting also counts as an "unfinished turn": for every active draft turn of this session, write a snapshot
  // that includes the failed state, so that after stopping, a refresh still shows the produced
  // greeting/analysis/preliminary tool stages instead of the whole turn disappearing.
  const sid = sessionId.value || 'default';
  for (const turnNum of [...activeDraftTurns]) {
    void writeDraftTurn(sid, turnNum);
  }
  // After aborting, the pending-approval card has already been handled by this approval flow; no need to show it again
  hitlRequest.value = null;
  isSending.value = false;
};

/** Input box draft (controlled, two-way bound to inputBox.vue via defineModel) */
const draft = ref('');

/**
 * Reference to the input box component instance: when the session is deleted (frontend broadcasts `SESSION_ABORT_STREAM_EVENT`),
 * call its `clearHistory()` to wipe the history cache left in this session's KeepAlive cache slot,
 * so history is strictly cleared along with the session deletion (even if that slot has not been LRU-evicted yet).
 */
const chatInputBoxRef = useTemplateRef<InstanceType<typeof ChatInputBox>>('chatInputBoxRef');

/**
 * Merge one streamed chunk into `chatMessages` (the single source of truth) by semantic type.
 *
 * This function is the shared message-rendering logic used by both `handleSend` (normal chat)
 * and the "HITL resume" path:
 * - text: if the tail of the same turn is an AI message, append to it; otherwise (tail is TOOL / different turn) create a new AI message
 * - tool_start: create a new TOOL message (status=running)
 * - tool_end: mark the most recent TOOL message of the same turn as done
 * - tool_result: fill the most recent same-turn TOOL message with args and result text, and mark its status per `error`
 *
 * The `turnNum` parameter bounds the scope, so unrelated history messages are never mistaken for the target
 * of the current streaming turn.
 *
 * @param sid Session id
 * @param content Chunk text (the body for text, the tool name for tool_start, the result text for tool_result)
 * @param type Semantic type
 * @param turnNum This turn's turn number (new messages are written into this turn)
 * @param meta Tool call metadata (only present for tool_result: tool_id/tool_name/args/error)
 */
const appendStreamChunk = (
  sid: string,
  content: string,
  type: AgentChunkType,
  turnNum: number,
  meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
) => {
  const last = chatMessages.value[chatMessages.value.length - 1];
  // Determine whether this belongs to an "active draft turn" (created on send, removed after onDone/error/stop).
  // On hit, write the draft in layers at the tail: text appends are debounced 200ms, discrete tool stages / first text are written immediately.
  const isActiveDraft = isDraftTurnActive(turnNum);
  if (type === 'text') {
    if (last && last.role === CHAT_ROLE.AI && last.turn_num === turnNum) {
      // Tail of same turn is AI → append the body
      last.content += content;
    } else {
      // Tail is TOOL / not this turn → create a new AI message to carry it
      chatMessages.value.push({
        session_id: sid,
        role: CHAT_ROLE.AI,
        content,
        reasoning: '',
        id: tempIdCounter++,
        turn_num: turnNum,
        timestamp: new Date().toISOString()
      });
    }
    if (isActiveDraft) scheduleDraftWrite(sid, turnNum);
  } else if (type === 'reasoning') {
    // Model thinking block: appended chunk by chunk into the `reasoning` field of the same-turn tail AI message,
    // without interfering with body text accumulation.
    // When the tail is TOOL / not this turn, create a new AI placeholder message to carry it (the body may arrive later).
    let target: MessageItem;
    if (last && last.role === CHAT_ROLE.AI && last.turn_num === turnNum) {
      target = last;
    } else {
      target = {
        session_id: sid,
        role: CHAT_ROLE.AI,
        content: '',
        reasoning: '',
        id: tempIdCounter++,
        turn_num: turnNum,
        timestamp: new Date().toISOString()
      };
      chatMessages.value.push(target);
    }
    target.reasoning = (target.reasoning ?? '') + content;
    // Thinking blocks are discrete stages; debouncing seems intuitive, but thinking content must be persisted in real time
    // with the stream to support refresh recovery, so it simply shares the text-append debounce path
    // (thinking blocks are usually not subdivided as frequently as body text).
    if (isActiveDraft) scheduleDraftWrite(sid, turnNum);
  } else if (type === 'tool_start') {
    chatMessages.value.push({
      session_id: sid,
      role: CHAT_ROLE.TOOL,
      content: '',
      toolName: content,
      toolStatus: 'running',
      // Args are delivered with meta at tool_start time, so call arguments can be viewed while running
      toolArgs: meta?.args ?? undefined,
      id: tempIdCounter++,
      turn_num: turnNum,
      timestamp: new Date().toISOString()
    });
    if (isActiveDraft) void commitDraftTurn(sid, turnNum);
  } else if (type === 'tool_end') {
    // Mark the most recent TOOL message of this turn as completed
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      const row = chatMessages.value[i];
      if (!row) continue;
      if (row.role === CHAT_ROLE.TOOL && row.turn_num === turnNum) {
        row.toolStatus = 'done';
        break;
      }
    }
    if (isActiveDraft) void commitDraftTurn(sid, turnNum);
  } else if (type === 'tool_result') {
    // Fill the most recent same-turn TOOL message with args and result text, and mark its status per `error`.
    // HITL resume special case: the interrupted tool card was created in the PREVIOUS (generate) turn,
    // while its tool_result arrives with the resume turn number (max+1) — the same-turn search misses it.
    // Fallback: the most recent TOOL card still 'running' (approve path) or 'failed' (reject path —
    // markRunningToolsFailed already ran before the resume frames arrive), so the execution result /
    // rejection notice lands on the card the user actually saw.
    let targetIdx = -1;
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      const row = chatMessages.value[i];
      if (!row) continue;
      if (row.role === CHAT_ROLE.TOOL && row.turn_num === turnNum) {
        targetIdx = i;
        break;
      }
    }
    if (targetIdx < 0) {
      for (let i = chatMessages.value.length - 1; i >= 0; i--) {
        const row = chatMessages.value[i];
        if (!row) continue;
        if (row.role === CHAT_ROLE.TOOL && (row.toolStatus === 'running' || row.toolStatus === 'failed')) {
          targetIdx = i;
          break;
        }
      }
    }
    const targetRow = targetIdx >= 0 ? chatMessages.value[targetIdx] : undefined;
    if (targetRow) {
      if (meta?.tool_name) targetRow.toolName = meta.tool_name;
      if (meta?.args) targetRow.toolArgs = meta.args;
      targetRow.toolResult = content;
      targetRow.toolStatus = meta?.error ? 'error' : 'done';
    }
    // tool_result is a discrete stage: persist immediately (keep preceding content whether success or error)
    if (isActiveDraft) void commitDraftTurn(sid, turnNum);
  }
  // Trigger a reactive update
  chatMessages.value = [...chatMessages.value];
};

/**
 * Mark all tool cards still in `running` state as failed (UI turns to a red ✗).
 *
 * Used by every path where "a tool call did not finish normally": HITL reject, user abort, stream error.
 * In none of these scenarios does the backend send back the corresponding tool_end; an unmarked card would spin forever.
 */
const markRunningToolsFailed = () => {
  let changed = false;
  chatMessages.value = chatMessages.value.map(m => {
    if (m.role === CHAT_ROLE.TOOL && m.toolStatus === 'running') {
      changed = true;
      return { ...m, toolStatus: 'failed' as const };
    }
    return m;
  });
  if (!changed) chatMessages.value = [...chatMessages.value];
};

/**
 * Discrete write-rate control for in-flight drafts.
 *
 * Key design: draft messages **reuse the real `turn_num` of their turn** (the same positive turn numbers
 * assigned by `handleSend`/HITL resume), rather than a separate negative draft turn number. Two reasons:
 *
 * 1. **Natural ordering**: `loadSessionHistory` sorts by `turn_num` ascending. Drafts reusing the real turn_num
 *    appear at their logical position (immediately after the in-flight/error turn), with no special handling.
 * 2. **Reconciliation dedup is feasible**: the server only persists a turn when the agent round completes fully
 *    (`aafter_agent`); in-flight / errored turns have **no rows at all** on the server, so a draft reusing that
 *    turn's turn_num never collides with already-persisted messages. During reconciliation, `serverRowFor` matches
 *    exactly on "same session + same turn_num + same role + same content", which is precisely how the server's
 *    positive-id row replaces the local draft's negative temporary-id row, achieving natural dedup.
 *
 * Therefore the `drafts` table uses `[session_id + turn_num]` as its primary key; repeatedly overwriting the same
 * turn is exactly the "cache every step" behavior.
 */

/**
 * Persist all messages of the given turn from the current `chatMessages` as one local draft.
 *
 * Only messages with `turn_num === turnNum` are saved, so turns unrelated to this send/this resume are never overwritten.
 *
 * @param sid      Session id
 * @param turnNum  This turn (the real turn_num used by stream callbacks)
 */
const writeDraftTurn = async (sid: string, turnNum: number) => {
  const rows = chatMessages.value.filter(m => m.turn_num === turnNum);
  if (rows.length === 0) return; // This turn has no messages yet; no need to write an empty draft
  // Deep copy: chatMessages is a Vue ref; elements are reactive Proxies after ref unwrapping.
  // With only a shallow spread / partial deep copy, nested images/audios/videos/toolArgs remain Proxy references,
  // and Dexie put() would throw DataCloneError during IndexedDB structured clone → the draft write fails.
  // MessageItem only contains JSON-compatible fields (no Date/Function/Blob), so a full JSON round-trip deep copy
  // is the safest, and it also prevents later streaming mutations from polluting the already-persisted draft.
  const snapshot = rows.map(m => JSON.parse(JSON.stringify(m)));
  try {
    await saveDraftTurn({ session_id: sid, turn_num: turnNum, messages: snapshot });
  } catch (e) {
    console.warn('[writeDraftTurn] 草稿写入失败：', sid, turnNum, e);
  }
};

/** 200ms debounce timer for text-append draft writes (key: `${sid}:${turnNum}`) */
const draftDebounceTimers = new Map<string, ReturnType<typeof setTimeout>>();

/**
 * Schedule one "text append" draft write (200ms trailing debounce).
 *
 * High-frequency text chunks are not persisted one by one but merged via the debounce; discrete stages
 * (send / each tool stage / error / first text / server completion) are written immediately by callers via `commitDraftTurn`.
 */
const scheduleDraftWrite = (sid: string, turnNum: number) => {
  const key = `${sid}:${turnNum}`;
  const existing = draftDebounceTimers.get(key);
  if (existing) clearTimeout(existing);
  draftDebounceTimers.set(
    key,
    setTimeout(() => {
      draftDebounceTimers.delete(key);
      void writeDraftTurn(sid, turnNum);
    }, 200)
  );
};

/**
 * Write the draft immediately (called at discrete stages) and cancel the turn's pending text debounce.
 * If a text append for this turn is still scheduled, flush one snapshot first before clearing the timer, avoiding duplicate writes.
 */
const commitDraftTurn = async (sid: string, turnNum: number) => {
  const key = `${sid}:${turnNum}`;
  const pending = draftDebounceTimers.get(key);
  if (pending) {
    clearTimeout(pending);
    draftDebounceTimers.delete(key);
  }
  await writeDraftTurn(sid, turnNum);
};

/**
 * Clear a turn's draft and cancel its pending debounce timer (called when the server successfully persists and reconciles, or when the session is cleared).
 */
const removeDraftTurn = (sid: string, turnNum: number) => {
  const key = `${sid}:${turnNum}`;
  const pending = draftDebounceTimers.get(key);
  if (pending) {
    clearTimeout(pending);
    draftDebounceTimers.delete(key);
  }
  void clearDraftTurn(sid, turnNum);
};

/**
 * Set of "active turns" currently streaming (elements are real turn_num values).
 *
 * `handleSend` and the "HITL resume" path each push one turn_num when starting a stream; it is removed when the
 * stream ends normally / errors / is aborted / is rejected. `appendStreamChunk` writes drafts only when turn_num
 * belongs to this set, so history rows never trigger accidental disk writes.
 */
const activeDraftTurns = new Set<number>();

/** Register one active draft turn (created on send, removed on completion). */
const trackDraftTurn = (sid: string, turnNum: number) => {
  activeDraftTurns.add(turnNum);
  // Write one draft at creation time, guaranteeing the fastest "cache-on-send" frame (user message + empty AI placeholder).
  void writeDraftTurn(sid, turnNum);
};

/** Determine whether a turn is in the active draft-persisting state. */
const isDraftTurnActive = (turnNum: number): boolean => activeDraftTurns.has(turnNum);

/**
 * Remove a turn's draft registration. Called after the server successfully persists: clear the draft + cancel the
 * pending debounce timer; the caller then triggers `loadSessionHistory` to replace local negative temporary-id rows
 * with the server's positive-id rows.
 */
const untrackDraftTurn = (sid: string, turnNum: number) => {
  const had = activeDraftTurns.delete(turnNum);
  removeDraftTurn(sid, turnNum);
  void had; // keep ref for clarity
};

/**
 * Handle input box send: add the user message to the list, and obtain the AI reply via a streaming request (Tauri IPC or browser WebSocket).
 *
 * Streamed replies are dynamically segmented: the backend distinguishes conversation text from tool calls by chunk type
 * (text / tool_start / tool_end); the frontend accordingly creates/updates separate message bubbles in real time —
 * one bubble for the conversation, one bubble per tool call.
 *
 * @param text User input content
 */
const handleSend = async (text: string) => {
  const sid = sessionId.value || 'default';

  // When the user sends a message, make sure the right side returns to the chat area (if it was previously on the background task list page)
  setTasksTabActive(false);

  // Compute the next turn number: current max turn_num + 1, not the array length.
  const turnNum = chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  // Carry the images pending send (taken and cleared from the pending list at send time)
  const imageBase64List = selectedImages.value.map(img => img.base64);
  // Carry the audios/videos pending send (taken and cleared from the pending list at send time)
  const audioBytesList = selectedAudios.value.map(a => a.base64);
  const videoBytesList = selectedVideos.value.map(v => v.base64);

  // Append the user message (displayed locally immediately)
  const userMsg: MessageItem = {
    session_id: sid,
    role: CHAT_ROLE.USER,
    content: text,
    images: imageBase64List,
    audios: audioBytesList,
    videos: videoBytesList,
    id: tempIdCounter++,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  // Initial AI placeholder message (content filled progressively by streamed chunks)
  const aiMsg: MessageItem = {
    session_id: sid,
    role: CHAT_ROLE.AI,
    content: '',
    reasoning: '',
    id: tempIdCounter++,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  chatMessages.value = [...chatMessages.value, userMsg, aiMsg];

  // After sending, clear the pending images/audios/videos and the input area
  selectedImages.value = [];
  selectedAudios.value = [];
  selectedVideos.value = [];
  draft.value = '';

  isSending.value = true;

  // Register this turn as an "active draft turn" so appendStreamChunk can persist accordingly;
  // the first registration immediately writes a "cache-on-send" frame (user message + empty AI placeholder).
  // Removed when the stream completes normally (onDone); kept on error/abort/reject so the draft caches the failed-stage content.
  trackDraftTurn(sid, turnNum);

  /**
   * Streamed chunk callback: reuse the shared `appendStreamChunk` to manage message segmentation dynamically by semantic type
   * (text/tool_start/tool_end), sharing the same rendering logic as the HITL resume path.
   */
  const onStreamChunk = (
    content: string,
    type: AgentChunkType,
    _sessionId: string,
    meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
  ) => {
    appendStreamChunk(sid, content, type, turnNum, meta);
  };

  try {
    const req: MultiModalMessage = { text };
    if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
    if (audioBytesList.length > 0) req.audio_bytes_list = audioBytesList;
    if (videoBytesList.length > 0) req.video_bytes_list = videoBytesList;
    activeAgentController = postAgentStream(
      sid,
      req,
      onStreamChunk,
      meta => {
        // Stream finished normally: first persist a final draft (guarding against last-moment text changes not yet written by the debounce),
        // then remove this turn's draft registration and trigger history reconciliation — the server has by now persisted this turn
        // as positive turn_num messages, and loadSessionHistory will replace the local negative temporary ids with the positive
        // server ids, deduplicating them.
        // Also attach the model metadata carried by the done frame (modelName/inputTokens/outputTokens) onto this turn's AI message.
        if (meta) {
          const ai = chatMessages.value.find(m => m.role === CHAT_ROLE.AI && m.turn_num === turnNum);
          if (ai) {
            if (meta.modelName !== undefined) ai.modelName = meta.modelName;
            if (meta.inputTokens !== undefined) ai.inputTokens = meta.inputTokens;
            if (meta.outputTokens !== undefined) ai.outputTokens = meta.outputTokens;
            chatMessages.value = [...chatMessages.value];
          }
        }
        void commitDraftTurn(sid, turnNum).then(() => {
          untrackDraftTurn(sid, turnNum);
          activeAgentController = null;
          isSending.value = false;
          void loadSessionHistory(sid);
        });
      },
      err => {
        // Stream error: in-flight tool calls did not finish normally, mark them as failed (red ✗).
        // The draft is **kept** — caching the already-completed greeting/analysis/preliminary tool stage content,
        // so it is not lost because the final result never arrived; the user still sees the pre-failure progress after refresh.
        activeAgentController = null;
        if (err instanceof StreamInterruptedError) {
          // Network stream loss (final failure after the reconnect budget is exhausted): content may have partially rendered,
          // so never overwrite existing body text with the failure message; only show the interruption hint when the AI body is empty.
          // Draft kept + one-shot reconciliation of the server-persisted result 25s later (the server may still be generating).
          if (!aiMsg.content) {
            aiMsg.content = t('errors.streamInterrupted');
          }
          markRunningToolsFailed();
          void writeDraftTurn(sid, turnNum);
          isSending.value = false;
          schedulePostInterruptReconcile(sid);
          return;
        }
        aiMsg.content = t('errors.replyFailed', { reason: String(err) });
        markRunningToolsFailed();
        // Persist a draft snapshot that includes the failed state
        void writeDraftTurn(sid, turnNum);
        isSending.value = false;
      },
      handleHitlRequest
    );
  } catch (e) {
    // Synchronous throw (rare); the stream never started, so just unlock directly.
    // Draft kept here as well: user message + empty AI placeholder + failure message are all cached.
    activeAgentController = null;
    aiMsg.content = t('errors.sendFailed', { reason: String(e) });
    void writeDraftTurn(sid, turnNum);
    isSending.value = false;
  }
};

/**
 * Selected images (base64, sent along with the message).
 * Only present during "local preview + send"; cleared after sending/canceling.
 */
const selectedImages = ref<{ base64: string; name: string }[]>([]);

/** Maximum number of images allowed per message */
const MAX_SELECTED_IMAGES = 10;

/** Hidden image file input */
const imageFileInput = useTemplateRef<HTMLInputElement>('imageFileInputRef');

/** Read an image file as a DataURL (includes the data:image/...;base64 prefix; strip the prefix before sending) */
const readImageFile = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

/** Remove one selected image */
const removeImage = (index: number) => {
  selectedImages.value.splice(index, 1);
  selectedImages.value = [...selectedImages.value];
};

/** Trigger the system image file picker */
const triggerImagePicker = () => {
  imageFileInput.value?.click();
};

/** Image selection callback: read as base64 and add to the pending-send list (capped at MAX_SELECTED_IMAGES) */
const onImageSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // Copy to a plain array snapshot before resetting input.value.
  // input.files is a "live" FileList — once value is emptied, the browser immediately clears that FileList;
  // reading files afterwards would yield an empty array, so selectedImages would never be populated and the preview would not show.
  const files = Array.from(input.files ?? []);
  input.value = ''; // Allow re-selecting the same file
  if (files.length === 0) return;

  // Count limit: truncate the excess and notify the user
  const remaining = MAX_SELECTED_IMAGES - selectedImages.value.length;
  if (remaining <= 0) {
    alert(t('chatInput.maxImages', { count: MAX_SELECTED_IMAGES }));
    return;
  }
  const accepted = files.slice(0, remaining);
  if (files.length > remaining) {
    alert(t('chatInput.maxImagesExceed', { count: MAX_SELECTED_IMAGES, extra: files.length - remaining }));
  }

  for (const file of accepted) {
    if (!file.type.startsWith('image/')) continue;
    try {
      const dataUrl = await readImageFile(file);
      // data:image/png;base64,xxxxx -> keep only the base64 part
      const base64 = dataUrl.split(',')[1] ?? '';
      selectedImages.value.push({ base64, name: file.name });
    } catch (e) {
      console.warn('[onImageSelected] 读取图片失败：', file.name, e);
    }
  }
  selectedImages.value = [...selectedImages.value];
};

/**
 * Selected audios (base64, sent along with the message).
 * Only present during "local preview + send"; cleared after sending/canceling.
 */
const selectedAudios = ref<{ base64: string; name: string }[]>([]);

/** Maximum number of audios allowed per message */
const MAX_SELECTED_AUDIOS = 5;

/** Hidden audio file input */
const audioFileInput = useTemplateRef<HTMLInputElement>('audioFileInputRef');

/** Read an audio file as a DataURL (includes the data:audio/...;base64 prefix; strip the prefix before sending) */
const readAudioFile = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

/** Remove one selected audio */
const removeAudio = (index: number) => {
  selectedAudios.value.splice(index, 1);
  selectedAudios.value = [...selectedAudios.value];
};

/** Trigger the system audio file picker */
const triggerAudioPicker = () => {
  audioFileInput.value?.click();
};

/** Audio selection callback: read as base64 and add to the pending-send list (capped at MAX_SELECTED_AUDIOS) */
const onAudioSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // Copy to a plain array snapshot before resetting input.value.
  // input.files is a "live" FileList — once value is emptied, the browser immediately clears that FileList;
  // reading files afterwards would yield an empty array, so selectedAudios would never be populated and the preview would not show.
  const files = Array.from(input.files ?? []);
  input.value = ''; // Allow re-selecting the same file
  if (files.length === 0) return;

  // Count limit: truncate the excess and notify the user
  const remaining = MAX_SELECTED_AUDIOS - selectedAudios.value.length;
  if (remaining <= 0) {
    alert(t('chatInput.maxAudios', { count: MAX_SELECTED_AUDIOS }));
    return;
  }
  const accepted = files.slice(0, remaining);
  if (files.length > remaining) {
    alert(t('chatInput.maxAudiosExceed', { count: MAX_SELECTED_AUDIOS, extra: files.length - remaining }));
  }

  for (const file of accepted) {
    if (!file.type.startsWith('audio/')) continue;
    try {
      const dataUrl = await readAudioFile(file);
      // data:audio/mpeg;base64,xxxxx -> keep only the base64 part
      const base64 = dataUrl.split(',')[1] ?? '';
      selectedAudios.value.push({ base64, name: file.name });
    } catch (e) {
      console.warn('[onAudioSelected] 读取音频失败：', file.name, e);
    }
  }
  selectedAudios.value = [...selectedAudios.value];
};

/**
 * Selected videos (base64, sent along with the message).
 * Only present during "local preview + send"; cleared after sending/canceling.
 */
const selectedVideos = ref<{ base64: string; name: string }[]>([]);

/** Maximum number of videos allowed per message */
const MAX_SELECTED_VIDEOS = 3;

/** Hidden video file input */
const videoFileInput = useTemplateRef<HTMLInputElement>('videoFileInputRef');

/** Read a video file as a DataURL (includes the data:video/...;base64 prefix; strip the prefix before sending) */
const readVideoFile = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

/** Remove one selected video */
const removeVideo = (index: number) => {
  selectedVideos.value.splice(index, 1);
  selectedVideos.value = [...selectedVideos.value];
};

/** Trigger the system video file picker */
const triggerVideoPicker = () => {
  videoFileInput.value?.click();
};

/** Video selection callback: read as base64 and add to the pending-send list (capped at MAX_SELECTED_VIDEOS) */
const onVideoSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // Copy to a plain array snapshot before resetting input.value (Safari handles this asynchronously, see onImageSelected).
  const files = Array.from(input.files ?? []);
  input.value = ''; // Allow re-selecting the same file
  if (files.length === 0) return;

  // Count limit: truncate the excess and notify the user
  const remaining = MAX_SELECTED_VIDEOS - selectedVideos.value.length;
  if (remaining <= 0) {
    alert(t('chatInput.maxVideos', { count: MAX_SELECTED_VIDEOS }));
    return;
  }
  const accepted = files.slice(0, remaining);
  if (files.length > remaining) {
    alert(t('chatInput.maxVideosExceed', { count: MAX_SELECTED_VIDEOS, extra: files.length - remaining }));
  }

  for (const file of accepted) {
    if (!file.type.startsWith('video/')) continue;
    try {
      const dataUrl = await readVideoFile(file);
      // data:video/mp4;base64,xxxxx -> keep only the base64 part
      const base64 = dataUrl.split(',')[1] ?? '';
      selectedVideos.value.push({ base64, name: file.name });
    } catch (e) {
      console.warn('[onVideoSelected] 读取视频失败：', file.name, e);
    }
  }
  selectedVideos.value = [...selectedVideos.value];
};

/** Tool trigger */
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

/** Load history for the specified session for this instance (clears local state, then rebuilds). Only called on new sessions / exception fallback paths. */
const doLoadFor = (sid: string) => {
  // When switching to / first-loading a new sid, clear all session-scoped local state first, then load this session's own history.
  // If chatMessages still holds the previous session's messages, even a loadSessionHistory dedup merge by id
  // would mix old and new messages together, causing "switched to the new session but the old session's content is displayed".
  chatMessages.value = [];
  isSending.value = false;
  // Discard the in-flight request controller left over from the previous session (its stream was invalidated by the session switch / is no longer usable)
  activeAgentController = null;
  activeHitlController?.abort();
  activeHitlController = null;
  // The previous session's pending-approval card / input draft / selected images must not leak into the new session
  hitlRequest.value = null;
  draft.value = '';
  selectedImages.value = [];
  selectedAudios.value = [];
  selectedVideos.value = [];
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
  // Cancel the pending "delayed post-interrupt reconciliation" timer
  if (postInterruptTimer) {
    clearTimeout(postInterruptTimer);
    postInterruptTimer = null;
  }
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
