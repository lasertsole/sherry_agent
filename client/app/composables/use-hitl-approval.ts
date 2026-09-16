/**
 * HITL (human-in-the-loop) approval slice for one session page: the pending
 * approval card state, the approve/reject decision flow (sent as a
 * `hitl_response` frame on the session's persistent socket) and the
 * three-tier-persistence card restore.
 */
import { ref } from 'vue';
import type { Ref } from 'vue';
import type { AgentChunkType, AgentSocket } from './bridge';
import type { MessageItem, HitlRequestData } from '../pages/home/type';
import type { ChatController } from './messages';
import type { StreamChunkMeta } from './use-stream-chunks';

/** Chunk-rendering surface required by the HITL resume flow. */
export interface HitlChunkSurface {
  /** Merge one streamed chunk into the message list. */
  appendStreamChunk: (
    sid: string,
    content: string,
    type: AgentChunkType,
    turnNum: number,
    meta?: StreamChunkMeta
  ) => void;
  /** Mark every still-running tool card as failed (red ✗). */
  markRunningToolsFailed: () => void;
}

/** Draft-persistence surface required by the HITL resume flow. */
export interface HitlDraftSurface {
  trackDraftTurn: (sid: string, turnNum: number) => void;
  writeDraftTurn: (sid: string, turnNum: number) => Promise<void>;
  commitDraftTurn: (sid: string, turnNum: number) => Promise<void>;
  untrackDraftTurn: (sid: string, turnNum: number) => void;
}

/** Collaborators of the HITL approval slice. */
export interface HitlApprovalDeps {
  /** The page's message list (single source of truth). */
  chatMessages: Ref<MessageItem[]>;
  /** Live session id of the page (route-derived). */
  sessionId: Ref<string>;
  /** Whether the page is currently streaming a generation. */
  isSending: Ref<boolean>;
  /** The ongoing send-generation controller (released when a resume takes over). */
  activeAgentController: Ref<ChatController | null>;
  /** The session's persistent agent socket (HITL resumes on the SAME socket). */
  socket: AgentSocket;
  /** Turn number currently receiving streamed chunks (send or HITL resume). */
  streamingTurn: Ref<number | null>;
  /** Rebuild the history list after a resume stream completes. */
  loadSessionHistory: (sid: string) => Promise<void>;
  /** Draft persistence slice. */
  drafts: HitlDraftSurface;
  /** Streamed-chunk rendering slice. */
  chunks: HitlChunkSurface;
}

/**
 * Create the HITL approval slice for one session page.
 * @param deps
 */
export function useHitlApproval(deps: HitlApprovalDeps) {
  const {
    chatMessages,
    sessionId,
    isSending,
    activeAgentController,
    socket,
    streamingTurn,
    loadSessionHistory,
    drafts,
    chunks
  } = deps;

  /** HITL approval request (set when agent pauses waiting for human approval) */
  const hitlRequest = ref<HitlRequestData | null>(null);

  /** Turn number of the in-flight HITL resume (null when none). */
  let activeResumeTurn: number | null = null;

  /**
   * Handle HITL approval request: show approval dialog
   * @param data
   */
  const handleHitlRequest = (data: HitlRequestData) => {
    hitlRequest.value = data;
  };

  /**
   * User approve/reject HITL request.
   *
   * The resume goes over the SAME persistent per-session socket (no fresh
   * WebSocket): `hitl_response` is sent on the existing connection, so chunks
   * and sequential `hitl_request` frames keep flowing through the page's
   * session-level handlers, and the approval still works after a refresh /
   * session switch / browser reopen (the socket reconnects on its own).
   * @param decision
   * @param message
   */
  const handleHitlDecision = (decision: 'approve' | 'approve_dir' | 'reject' | 'yolo', message: string = '') => {
    const sid = sessionId.value;
    if (!sid) {
      hitlRequest.value = null;
      return;
    }
    // Sequential HITL: a previous resume may still be streaming. Stop it so every
    // click has a real channel (never a silent no-op) before starting the new one.
    if (activeResumeTurn !== null) {
      void socket.stop();
      activeResumeTurn = null;
    }

    // Record the turn number for this approval: new messages from resume go to 'current max turn + 1'
    const turnNum = chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;
    activeResumeTurn = turnNum;
    streamingTurn.value = turnNum;
    isSending.value = true;

    // Register this resume turn as draft (consistent with handleSend), so appendStreamChunk can write to disk in real-time;
    // removed during reconciliation when resume stream completes normally, retained to cache the failure stage on reject/failure.
    drafts.trackDraftTurn(sid, turnNum);

    // Reject: this tool won't be executed, backend won't send back tool_end, so mark the currently still running
    // tool card as failed (UI changes from spinner to red ✗), avoid permanent loading state.
    if (decision === 'reject') {
      chunks.markRunningToolsFailed();
      void drafts.writeDraftTurn(sid, turnNum);
    }

    // Resume over the persistent socket — no new connection, no socket close.
    socket.sendHitlResponse({ decision, message });

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
   * @param sid
   */
  const restorePendingHitl = async (sid: string) => {
    if (!sid) return;
    // Do not re-raise if an approval is already in flight or a card already exists
    if (hitlRequest.value || activeResumeTurn !== null) return;
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

  /**
   * Whether the given turn belongs to the in-flight HITL resume.
   * @param turnNum
   */
  const isResumeTurn = (turnNum: number | null): boolean => turnNum !== null && turnNum === activeResumeTurn;

  /**
   * The resume turn finished successfully: commit its draft and reconcile.
   * @param turnNum
   */
  const onTurnFinished = (turnNum: number) => {
    if (turnNum !== activeResumeTurn) return;
    activeResumeTurn = null;
    const sid = sessionId.value || 'default';
    void drafts.commitDraftTurn(sid, turnNum).then(() => {
      drafts.untrackDraftTurn(sid, turnNum);
      activeAgentController.value = null;
      isSending.value = false;
      void loadSessionHistory(sid);
    });
  };

  /** The resume turn failed: release the controller slot and unlock the input. */
  const onTurnError = () => {
    activeResumeTurn = null;
    activeAgentController.value = null;
    isSending.value = false;
  };

  /** Abort the in-flight HITL resume, if any (sends `{type:'stop'}` on the same socket). */
  const abortResume = () => {
    if (activeResumeTurn === null) return;
    void socket.stop();
    if (streamingTurn.value === activeResumeTurn) streamingTurn.value = null;
    activeResumeTurn = null;
  };

  /** Close the pending-approval card without answering it. */
  const clearRequest = () => {
    hitlRequest.value = null;
  };

  return {
    hitlRequest,
    handleHitlRequest,
    handleHitlDecision,
    restorePendingHitl,
    isResumeTurn,
    onTurnFinished,
    onTurnError,
    abortResume,
    clearRequest
  };
}
