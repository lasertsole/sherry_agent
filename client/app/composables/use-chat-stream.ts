/**
 * Streaming chat slice for one session page: sending a message (streamed via
 * the unified bridge), stopping a generation, WS reconnect banner + queue
 * badge state and the post-interrupt delayed reconciliation.
 */
import { computed, onUnmounted, ref, toRaw } from 'vue';
import type { Ref } from 'vue';
import type { Composer } from 'vue-i18n';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem, HitlRequestData } from '../pages/home/type';
import type { MultiModalMessage } from '@/types/message';
import type { ChatController } from './messages';
import type { AgentChunkType, AgentSocket, QueuedInfo, TurnStartedInfo } from './bridge';
import type { StreamChunkMeta } from './use-stream-chunks';
import type { DraftPersistence } from './use-draft-persistence';

/** One locally-registered send, keyed by its protocol `msg_id`. */
interface SendEntry {
  turnNum: number;
  userMsg: MessageItem;
  aiMsg: MessageItem;
}

/** One queued badge (a queued send awaiting its turn). */
interface QueueBadge {
  msgId: string;
  position: number;
  queueSize: number;
  turn: number;
}

/** Generate a protocol `msg_id` (RFC4122 v4 UUID, with a non-crypto fallback). */
function generateMsgId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

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
  /** The session's persistent agent socket (acquired once by the page). */
  socket: AgentSocket;
  /** Turn number currently receiving streamed chunks (send or HITL resume). */
  streamingTurn: Ref<number | null>;
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
    /** Whether the given turn belongs to an in-flight HITL resume. */
    isResumeTurn: (turnNum: number | null) => boolean;
    /** The resume turn finished successfully (HITL slice owns its commit). */
    onTurnFinished: (turnNum: number) => void;
    /** The resume turn failed. */
    onTurnError: () => void;
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
    socket,
    streamingTurn,
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

  /** Locally-registered sends, keyed by protocol `msg_id` (for `turn_started` routing). */
  const sendByMsgId = new Map<string, SendEntry>();
  /** Earlier member turns of a batch, keyed by the batch's trailing turn. */
  const batchMembers = new Map<number, number[]>();
  /** Most recently sent `msg_id` (fallback for `queued` frames without an id). */
  let lastSendMsgId: string | null = null;

  /**
   * Queue badges (one per queued send; a batch may enqueue several).
   * Data source is the backend `queued` frame for a send issued while the session
   * was still streaming (the message streams once earlier turns finish). Badges are
   * keyed by `msgId`, so `turn_started` can clear ALL badges of the turn's member
   * sends and a concurrent earlier stream can never wipe a newer badge.
   */
  const queueBadges = ref<QueueBadge[]>([]);

  /** Aggregate badge shown in the input toolbar (the queued send closest to running). */
  const queueBadge = computed<{ position: number; queueSize: number } | null>(() => {
    if (queueBadges.value.length === 0) return null;
    const next = queueBadges.value.reduce((min, badge) => (badge.position < min.position ? badge : min));
    return { position: next.position, queueSize: next.queueSize };
  });

  /**
   * `queued` frame → queue badge. Only drives THIS instance's badge: matched against the frozen
   * `mySid`, not the live `sessionId` computed — the latter reads the globally shared route object
   * and flips to another sid while this KeepAlive-cached instance sits in the background.
   * @param info
   */
  const handleQueued = (info: QueuedInfo) => {
    if (info.sessionId !== mySid) return;
    const msgId = info.messageId ?? lastSendMsgId ?? '';
    const turn = (msgId ? sendByMsgId.get(msgId)?.turnNum : undefined) ?? streamingTurn.value ?? 0;
    queueBadges.value = [
      ...queueBadges.value.filter(badge => badge.msgId !== msgId),
      { msgId, position: info.position, queueSize: info.queueSize, turn }
    ];
  };

  /**
   * Drop the badges belonging to the given turn (turn-scoped clear helper).
   * @param turnNum
   */
  const clearQueueBadgeForTurn = (turnNum: number | null) => {
    if (turnNum === null) return;
    queueBadges.value = queueBadges.value.filter(badge => badge.turn !== turnNum);
  };

  /**
   * Drop the badges of the given member `msg_id`s (turn_started consolidation).
   * @param msgIds
   */
  const clearQueueBadgesFor = (msgIds: string[]) => {
    const ids = new Set(msgIds);
    queueBadges.value = queueBadges.value.filter(badge => !ids.has(badge.msgId));
  };

  /** Drop every queue badge unconditionally (used when the stream/session is torn down). */
  const clearQueueBadge = () => {
    queueBadges.value = [];
  };

  /**
   * Session-level chunk handler: every `chunk` frame of this session is routed to
   * the turn currently receiving the reply (set by `handleSend` and updated by
   * `turn_started`). This is the handler the page installs once.
   *
   * This handler must NEVER touch the queue badges: `streamingTurn` points at the
   * turn that is streaming NOW, while a newer send of this session may already be
   * queued (badge visible) but not started; a chunk of the running turn must not
   * wipe that queued send's badge. Badge clearing is owned by `handleTurnStarted`
   * (all member badges of the starting batch) and by the done/error/stop paths.
   * @param content
   * @param type
   * @param _sessionId
   * @param meta
   */
  const handleSocketChunk = (content: string, type: AgentChunkType, _sessionId: string, meta?: StreamChunkMeta) => {
    const turn = streamingTurn.value;
    if (turn === null) return;
    chunks.appendStreamChunk(mySid, content, type, turn, meta);
  };

  /**
   * `turn_started` handler: a generation turn (idle single-send OR a batch of queued
   * sends) began.
   *
   * Consolidation: the backend persists a batch as ONE turn (the next turn after the
   * running turn), so every member is unified onto the EARLIEST member's turn — the
   * server's batch turn. The batch renders as N user bubbles + exactly ONE trailing
   * AI reply: the earlier members' EMPTY AI placeholders are removed, the trailing
   * member's bubble receives the whole reply, and every member queue badge is cleared.
   * Unifying the turn numbers keeps the live view identical to the canonical history
   * (a reload no longer changes anything).
   * @param info
   */
  const handleTurnStarted = (info: TurnStartedInfo) => {
    if (info.sessionId !== mySid) return;
    const members = info.messageIds
      .map(id => sendByMsgId.get(id))
      .filter((entry): entry is SendEntry => entry !== undefined)
      .sort((a, b) => a.turnNum - b.turnNum);
    if (members.length === 0) return;

    const unifiedTurn = members[0]!.turnNum;
    const replyTarget = members[members.length - 1]!;
    for (const member of members) {
      // Collapse the earlier sends' draft turns into the unified turn so the done
      // handler commits the batch exactly once.
      if (member.turnNum !== unifiedTurn) drafts.untrackDraftTurn(mySid, member.turnNum);
      member.turnNum = unifiedTurn;
      member.userMsg.turn_num = unifiedTurn;
      member.aiMsg.turn_num = unifiedTurn;
      // Drop the non-trailing EMPTY AI placeholders; the trailing one receives the
      // whole reply (it already sits after every member user bubble).
      if (member !== replyTarget && !member.aiMsg.content && !(member.aiMsg.reasoning ?? '')) {
        // `chatMessages.value` elements are Vue proxies while `member.aiMsg` is the
        // raw object, so compare through `toRaw` (a plain `!==` never matched and
        // left the earlier placeholders in place).
        chatMessages.value = chatMessages.value.filter(row => toRaw(row) !== member.aiMsg);
      }
    }
    clearQueueBadgesFor(info.messageIds);
    streamingTurn.value = unifiedTurn;
    chatMessages.value = [...chatMessages.value];
  };

  /**
   * Drop the send registry entries of a finished turn (and its batch members).
   * @param turnNum
   * @param memberTurns
   */
  const dropSendEntries = (turnNum: number, memberTurns: number[]) => {
    const turns = new Set<number>([turnNum, ...memberTurns]);
    for (const [msgId, entry] of sendByMsgId) {
      if (turns.has(entry.turnNum)) sendByMsgId.delete(msgId);
    }
  };

  /**
   * Attach the model metadata carried by the done frame onto the turn's AI message.
   * @param turnNum
   * @param meta
   * @param meta.modelName
   * @param meta.inputTokens
   * @param meta.outputTokens
   */
  const attachDoneMeta = (
    turnNum: number,
    meta?: { modelName?: string; inputTokens?: number; outputTokens?: number }
  ) => {
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
   * Session-level `done` handler: the generation turn finished successfully.
   * Commits the turn's draft, untracks the batch members and reconciles history.
   * A HITL resume turn is completed by the HITL slice instead.
   * @param meta
   * @param meta.modelName
   * @param meta.inputTokens
   * @param meta.outputTokens
   */
  const handleSocketDone = (meta?: { modelName?: string; inputTokens?: number; outputTokens?: number }) => {
    const turn = streamingTurn.value;
    if (turn === null) return;
    if (hitl.isResumeTurn(turn)) {
      hitl.onTurnFinished(turn);
      streamingTurn.value = null;
      return;
    }
    attachDoneMeta(turn, meta);
    clearQueueBadgeForTurn(turn);
    const members = batchMembers.get(turn) ?? [];
    batchMembers.delete(turn);
    void drafts.commitDraftTurn(mySid, turn).then(() => {
      drafts.untrackDraftTurn(mySid, turn);
      for (const memberTurn of members) drafts.untrackDraftTurn(mySid, memberTurn);
      dropSendEntries(turn, members);
      activeAgentController.value = null;
      isSending.value = false;
      streamingTurn.value = null;
      void loadSessionHistory(mySid);
    });
  };

  /**
   * Session-level error handler: mark running tools failed and surface the failure
   * on the turn's AI bubble.
   * @param err
   */
  const handleSocketError = (err: unknown) => {
    const turn = streamingTurn.value;
    activeAgentController.value = null;
    clearQueueBadgeForTurn(turn);
    const aiMsg =
      turn === null ? undefined : chatMessages.value.find(m => m.role === CHAT_ROLE.AI && m.turn_num === turn);
    const isResume = hitl.isResumeTurn(turn);
    if (err instanceof StreamInterruptedError) {
      // Network stream loss (final failure after the reconnect budget is exhausted): content may have partially rendered,
      // so never overwrite existing body text with the failure message; only show the interruption hint when the AI body is empty.
      // Draft kept + one-shot reconciliation of the server-persisted result 25s later (the server may still be generating).
      if (aiMsg && !aiMsg.content) {
        aiMsg.content = t('errors.streamInterrupted');
        chatMessages.value = [...chatMessages.value];
      }
      chunks.markRunningToolsFailed();
      if (turn !== null) void drafts.writeDraftTurn(mySid, turn);
      if (isResume) hitl.onTurnError();
      else schedulePostInterruptReconcile(mySid);
      isSending.value = false;
      streamingTurn.value = null;
      return;
    }
    if (aiMsg) {
      aiMsg.content = t('errors.replyFailed', { reason: String(err) });
      chatMessages.value = [...chatMessages.value];
    }
    chunks.markRunningToolsFailed();
    // Persist a draft snapshot that includes the failed state
    if (turn !== null) {
      void drafts.writeDraftTurn(mySid, turn);
      dropSendEntries(turn, batchMembers.get(turn) ?? []);
      batchMembers.delete(turn);
    }
    if (isResume) hitl.onTurnError();
    isSending.value = false;
    streamingTurn.value = null;
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
    // 128K MAX_TOKEN guard: warn (non-blocking) when the backend config is
    // invalid; the warning stays silent when valid or when the fetch fails.
    try {
      const cfg = await getModelConfigCached();
      if (!cfg.valid) {
        toastWarn(t('chat.tokenGuard.title'), t('chat.tokenGuard.detail'), 6000);
      }
    } catch {
      // Config fetch failure (backend down, etc.) — stay silent, never block sending.
    }

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

    // Capture the busy flag BEFORE flipping `isSending`: a send issued while the
    // session is already streaming is QUEUED (the backend confirms with a `queued`
    // frame) and does NOT start a turn now. Overwriting `streamingTurn` with the
    // queued turn would misroute the running turn's remaining chunks AND make the
    // running turn's chunks clear the queued send's badge. Only an idle send starts
    // a turn immediately; `turn_started` moves `streamingTurn` to the batch's
    // trailing turn once the queued batch actually starts.
    const wasSending = isSending.value;
    isSending.value = true;
    if (!wasSending) streamingTurn.value = turnNum;

    // Register this turn as an "active draft turn" so appendStreamChunk can persist accordingly;
    // the first registration immediately writes a "cache-on-send" frame (user message + empty AI placeholder).
    // Removed when the stream completes normally (onDone); kept on error/abort/reject so the draft caches the failed-stage content.
    drafts.trackDraftTurn(sid, turnNum);

    // Frozen protocol: every send carries a client-generated `msg_id`; record it so
    // `turn_started.message_ids` / `queued.message_id` map back to this local turn.
    const msgId = generateMsgId();
    lastSendMsgId = msgId;
    sendByMsgId.set(msgId, { turnNum, userMsg, aiMsg });

    try {
      const req: MultiModalMessage = { text };
      if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
      if (audioBytesList.length > 0) req.audio_bytes_list = audioBytesList;
      if (videoBytesList.length > 0) req.video_bytes_list = videoBytesList;
      activeAgentController.value = postAgentStream(
        sid,
        req,
        handleSocketChunk,
        handleSocketDone,
        handleSocketError,
        hitl.handleHitlRequest,
        handleQueued,
        msgId
      );
    } catch (e) {
      // Synchronous throw (rare); the stream never started, so just unlock directly.
      // Draft kept here as well: user message + empty AI placeholder + failure message are all cached.
      activeAgentController.value = null;
      sendByMsgId.delete(msgId);
      aiMsg.content = t('errors.sendFailed', { reason: String(e) });
      chatMessages.value = [...chatMessages.value];
      void drafts.writeDraftTurn(sid, turnNum);
      streamingTurn.value = null;
      isSending.value = false;
    }
  };

  /** Stop the current AI reply generation: send `{type:'stop'}` on the SAME socket (never close it). */
  const handleStop = () => {
    // Session-level stop: one frame halts the session's generation, and every
    // in-flight send of this session settles as aborted on the shared socket.
    void socket.stop();
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
    // Every queued badge belongs to the aborted sends: drop them so they cannot linger
    clearQueueBadge();
    streamingTurn.value = null;
    isSending.value = false;
  };

  return {
    handleSend,
    handleStop,
    handleSocketChunk,
    handleTurnStarted,
    handleQueued,
    handleSocketDone,
    handleSocketError,
    reconnectState,
    queueBadge,
    clearQueueBadge,
    onStreamReconnecting,
    onStreamReconnected,
    onStreamReconnectFailed
  };
}
