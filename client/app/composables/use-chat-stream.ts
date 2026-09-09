/**
 * Streaming chat slice for one session page: sending a message (streamed via
 * the unified bridge), stopping a generation, WS reconnect banner + queue
 * badge state and the post-interrupt delayed reconciliation.
 */
import { onUnmounted, ref } from 'vue';
import type { Ref } from 'vue';
import type { Composer } from 'vue-i18n';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem, HitlRequestData } from '../pages/home/type';
import type { MultiModalMessage } from '@/types/message';
import type { ChatController } from './messages';
import type { AgentChunkType, QueuedInfo } from './bridge';
import type { StreamChunkMeta } from './use-stream-chunks';
import type { DraftPersistence } from './use-draft-persistence';

/** Collaborators of the chat stream slice. */
export interface ChatStreamDeps {
  /** The page's message list (single source of truth). */
  chatMessages: Ref<MessageItem[]>;
  /** Live session id of the page (route-derived). */
  sessionId: Ref<string>;
  /** This page instance's frozen session id (KeepAlive: one instance per sid). */
  mySid: string;
  /** Input box draft (two-way bound to the input component). */
  draft: Ref<string>;
  /** Whether the page is currently streaming a generation. */
  isSending: Ref<boolean>;
  /** The ongoing send-generation controller. */
  activeAgentController: Ref<ChatController | null>;
  /** i18n translator. */
  t: Composer['t'];
  /** Snapshot the pending media base64 payloads (taken at send time). */
  getPendingMedia: () => { images: string[]; audios: string[]; videos: string[] };
  /** Clear the pending media selections (called after a send takes them). */
  clearMediaSelection: () => void;
  /** Switch the right-side view back to chat (no-op passthrough to the tasks tab state). */
  setTasksTabActive: (active: boolean) => void;
  /** Rebuild the history list after the stream completes. */
  loadSessionHistory: (sid: string) => Promise<void>;
  /** Draft persistence slice. */
  drafts: Pick<
    DraftPersistence,
    | 'allocateTempId'
    | 'activeTurns'
    | 'trackDraftTurn'
    | 'writeDraftTurn'
    | 'commitDraftTurn'
    | 'untrackDraftTurn'
    | 'isDraftTurnActive'
  >;
  /** Streamed-chunk rendering slice. */
  chunks: {
    appendStreamChunk: (
      sid: string,
      content: string,
      type: AgentChunkType,
      turnNum: number,
      meta?: StreamChunkMeta
    ) => void;
    markRunningToolsFailed: () => void;
  };
  /** HITL approval slice (approval requests surface through the pending card). */
  hitl: {
    handleHitlRequest: (data: HitlRequestData) => void;
    abortResume: () => void;
    clearRequest: () => void;
  };
}

/**
 * Create the streaming chat slice for one session page.
 * @param deps
 */
export function useChatStream(deps: ChatStreamDeps) {
  const {
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
  } = deps;

  /**
   * WS stream reconnection status banner: null = not reconnecting; otherwise show 'Reconnecting (attempt/max times)'.
   * Data source is mitt events broadcast by bridge's sendChatMessageWs during exponential backoff reconnection
   * (stream:reconnecting / stream:reconnected / stream:reconnect:failed).
   */
  const reconnectState = ref<{ attempt: number; max: number } | null>(null);

  /**
   * Reconnection events only drive this session's banner (route may cache multiple session instances simultaneously)
   * @param event
   */
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
   * Queue badge state: null = nothing queued; otherwise show 'Queued · position N of M'.
   * Data source is bridge.sendChatMessageWs forwarding the backend `queued` frame for a send
   * issued while the session was still streaming (the message will stream once earlier turns finish).
   * `turn` records which local turn the badge belongs to, so clears are turn-scoped and a
   * concurrently finishing earlier stream can never wipe a newer queued send's badge.
   */
  const queueBadge = ref<{ position: number; queueSize: number; turn: number } | null>(null);

  /**
   * `queued` frame → queue badge. Only drives THIS instance's badge: matched against the frozen
   * `mySid`, not the live `sessionId` computed — the latter reads the globally shared route object
   * and flips to another sid while this KeepAlive-cached instance sits in the background.
   * @param info
   * @param turnNum
   */
  const handleQueued = (info: QueuedInfo, turnNum: number) => {
    if (info.sessionId !== mySid) return;
    queueBadge.value = { position: info.position, queueSize: info.queueSize, turn: turnNum };
  };

  /**
   * Drop the queue badge when it belongs to the given turn (turn-scoped clear helper).
   * @param turnNum
   */
  const clearQueueBadgeForTurn = (turnNum: number) => {
    if (queueBadge.value?.turn === turnNum) {
      queueBadge.value = null;
    }
  };

  /** Drop the queue badge unconditionally (used when the stream/session is torn down). */
  const clearQueueBadge = () => {
    queueBadge.value = null;
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

  // Cancel the pending "delayed post-interrupt reconciliation" timer on teardown
  onUnmounted(() => {
    if (postInterruptTimer) {
      clearTimeout(postInterruptTimer);
      postInterruptTimer = null;
    }
  });

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

    // Carry the images/audios/videos pending send (taken from the pending lists at send time)
    const pendingMedia = getPendingMedia();
    const imageBase64List = pendingMedia.images;
    const audioBytesList = pendingMedia.audios;
    const videoBytesList = pendingMedia.videos;

    // Append the user message (displayed locally immediately)
    const userMsg: MessageItem = {
      session_id: sid,
      role: CHAT_ROLE.USER,
      content: text,
      images: imageBase64List,
      audios: audioBytesList,
      videos: videoBytesList,
      id: drafts.allocateTempId(),
      turn_num: turnNum,
      timestamp: new Date().toISOString()
    };

    // Initial AI placeholder message (content filled progressively by streamed chunks)
    const aiMsg: MessageItem = {
      session_id: sid,
      role: CHAT_ROLE.AI,
      content: '',
      reasoning: '',
      id: drafts.allocateTempId(),
      turn_num: turnNum,
      timestamp: new Date().toISOString()
    };

    chatMessages.value = [...chatMessages.value, userMsg, aiMsg];

    // After sending, clear the pending images/audios/videos and the input area
    clearMediaSelection();
    draft.value = '';

    isSending.value = true;

    // Register this turn as an "active draft turn" so appendStreamChunk can persist accordingly;
    // the first registration immediately writes a "cache-on-send" frame (user message + empty AI placeholder).
    // Removed when the stream completes normally (onDone); kept on error/abort/reject so the draft caches the failed-stage content.
    drafts.trackDraftTurn(sid, turnNum);

    /**
     * Streamed chunk callback: reuse the shared `appendStreamChunk` to manage message segmentation dynamically by semantic type
     * (text/tool_start/tool_end), sharing the same rendering logic as the HITL resume path.
     * First chunk of this turn = the (possibly queued) message started streaming: drop this turn's
     * queue badge. Turn-scoped, so chunks of an earlier in-flight stream never clear a newer badge.
     * @param content
     * @param type
     * @param _sessionId
     * @param meta
     */
    const onStreamChunk = (content: string, type: AgentChunkType, _sessionId: string, meta?: StreamChunkMeta) => {
      clearQueueBadgeForTurn(turnNum);
      chunks.appendStreamChunk(sid, content, type, turnNum, meta);
    };

    /**
     * Attach the model metadata carried by the done frame onto this turn's AI message.
     * @param meta
     * @param meta.modelName
     * @param meta.inputTokens
     * @param meta.outputTokens
     */
    const attachDoneMeta = (meta?: { modelName?: string; inputTokens?: number; outputTokens?: number }) => {
      if (!meta) return;
      const ai = chatMessages.value.find(m => m.role === CHAT_ROLE.AI && m.turn_num === turnNum);
      if (ai) {
        if (meta.modelName !== undefined) ai.modelName = meta.modelName;
        if (meta.inputTokens !== undefined) ai.inputTokens = meta.inputTokens;
        if (meta.outputTokens !== undefined) ai.outputTokens = meta.outputTokens;
        chatMessages.value = [...chatMessages.value];
      }
    };

    /**
     * Stream finished normally: persist the final draft, untrack the turn and reconcile against the server.
     * @param meta
     * @param meta.modelName
     * @param meta.inputTokens
     * @param meta.outputTokens
     */
    const onStreamDone = (meta?: { modelName?: string; inputTokens?: number; outputTokens?: number }) => {
      clearQueueBadgeForTurn(turnNum);
      attachDoneMeta(meta);
      void drafts.commitDraftTurn(sid, turnNum).then(() => {
        drafts.untrackDraftTurn(sid, turnNum);
        activeAgentController.value = null;
        isSending.value = false;
        void loadSessionHistory(sid);
      });
    };

    /**
     * Stream error: mark running tools failed and surface the failure on the AI placeholder.
     * @param err
     */
    const onStreamError = (err: unknown) => {
      activeAgentController.value = null;
      // This turn will never stream: drop its queue badge (covers both error branches below).
      clearQueueBadgeForTurn(turnNum);
      if (err instanceof StreamInterruptedError) {
        // Network stream loss (final failure after the reconnect budget is exhausted): content may have partially rendered,
        // so never overwrite existing body text with the failure message; only show the interruption hint when the AI body is empty.
        // Draft kept + one-shot reconciliation of the server-persisted result 25s later (the server may still be generating).
        if (!aiMsg.content) {
          aiMsg.content = t('errors.streamInterrupted');
        }
        chunks.markRunningToolsFailed();
        void drafts.writeDraftTurn(sid, turnNum);
        isSending.value = false;
        schedulePostInterruptReconcile(sid);
        return;
      }
      aiMsg.content = t('errors.replyFailed', { reason: String(err) });
      chunks.markRunningToolsFailed();
      // Persist a draft snapshot that includes the failed state
      void drafts.writeDraftTurn(sid, turnNum);
      isSending.value = false;
    };

    /**
     * Backend enqueued this send (session was busy): show the queue badge above the input box.
     * @param info
     */
    const onStreamQueued = (info: QueuedInfo) => {
      handleQueued(info, turnNum);
    };

    try {
      const req: MultiModalMessage = { text };
      if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
      if (audioBytesList.length > 0) req.audio_bytes_list = audioBytesList;
      if (videoBytesList.length > 0) req.video_bytes_list = videoBytesList;
      activeAgentController.value = postAgentStream(
        sid,
        req,
        onStreamChunk,
        onStreamDone,
        onStreamError,
        hitl.handleHitlRequest,
        onStreamQueued
      );
    } catch (e) {
      // Synchronous throw (rare); the stream never started, so just unlock directly.
      // Draft kept here as well: user message + empty AI placeholder + failure message are all cached.
      activeAgentController.value = null;
      aiMsg.content = t('errors.sendFailed', { reason: String(e) });
      void drafts.writeDraftTurn(sid, turnNum);
      isSending.value = false;
    }
  };

  /** Stop the current AI reply generation (local frontend abort + notify the backend to stop) */
  const handleStop = () => {
    activeAgentController.value?.abort();
    activeAgentController.value = null;
    // If a HITL resume stream recovery is in flight, abort that controller as well
    // (its abort sends {type:'stop'} to the backend, making answering=False and triggering a CancelledError)
    hitl.abortResume();
    // The aborted turn did not finish; mark the still-running tool cards as failed (red ✗)
    chunks.markRunningToolsFailed();
    // Aborting also counts as an "unfinished turn": for every active draft turn of this session, write a snapshot
    // that includes the failed state, so that after stopping, a refresh still shows the produced
    // greeting/analysis/preliminary tool stages instead of the whole turn disappearing.
    const sid = sessionId.value || 'default';
    for (const turnNum of drafts.activeTurns()) {
      void drafts.writeDraftTurn(sid, turnNum);
    }
    // After aborting, the pending-approval card has already been handled by this approval flow; no need to show it again
    hitl.clearRequest();
    // The queued badge (if any) belongs to the aborted send: drop it so it cannot linger
    clearQueueBadge();
    isSending.value = false;
  };

  return {
    handleSend,
    handleStop,
    reconnectState,
    queueBadge,
    clearQueueBadge,
    onStreamReconnecting,
    onStreamReconnected,
    onStreamReconnectFailed
  };
}
