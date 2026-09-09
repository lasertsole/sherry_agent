/**
 * HITL (human-in-the-loop) approval slice for one session page: the pending
 * approval card state, the approve/reject decision flow (via an independent
 * `resumeHitl` WebSocket) and the three-tier-persistence card restore.
 */
import { ref } from 'vue';
import type { Ref } from 'vue';
import type { AgentChunkType } from './bridge';
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
  const { chatMessages, sessionId, isSending, activeAgentController, loadSessionHistory, drafts, chunks } = deps;

  /** HITL approval request (set when agent pauses waiting for human approval) */
  const hitlRequest = ref<HitlRequestData | null>(null);

  /** Ongoing HITL resume controller (single-flight: only one allowed per session) */
  let activeHitlController: { closed: boolean; abort: () => void } | null = null;

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
   * Decision no longer depends on `sendHitlResponse` mounted on the closure returned by `streamChatMessage` during real-time message sending —
   * that closure is only available when `!done && socket.readyState === OPEN`,
   * after page refresh/session switch/browser reopen socket is closed、controller is null, approval will silently no-op.
   * Here changed to independent `resumeHitl`: directly open a new WS to backend `/sessions/agent/ws`,
   * send `hitl_response` frame to streamingly restore agent from LangGraph checkpoint, thus
   * supporting three-layer persistence (session switch, refresh, browser reopen) and still being able to complete approval.
   * @param decision
   * @param message
   */
  const handleHitlDecision = (decision: 'approve' | 'reject' | 'yolo', message: string = '') => {
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
    drafts.trackDraftTurn(sid, turnNum);

    const onChunk = (content: string, type: AgentChunkType, _sessionId: string, meta?: StreamChunkMeta) => {
      chunks.appendStreamChunk(sid, content, type, turnNum, meta);
    };

    const { controller, promise } = resumeHitl(sid, decision, message, onChunk, handleHitlRequest);
    activeHitlController = controller;

    // Reject: this tool won't be executed, backend won't send back tool_end, so mark the currently still running
    // tool card as failed (UI changes from spinner to red ✗), avoid permanent loading state.
    if (decision === 'reject') {
      chunks.markRunningToolsFailed();
      // Reject won't trigger backend response, immediately write draft with failed status to disk, ensuring failed progress is visible after refresh
      void drafts.writeDraftTurn(sid, turnNum);
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
      activeAgentController.value = null;
      isSending.value = false;
      // If no new hitl_request is triggered during approval, close the approval card
      if (hitlRequest.value) {
        hitlRequest.value = null;
      }
    };
    promise
      .then(() => {
        // Normal completion: write final draft first then reconcile remove (consistent with handleSend onDone)
        return drafts.commitDraftTurn(sid, turnNum).then(() => {
          drafts.untrackDraftTurn(sid, turnNum);
          finish();
          void loadSessionHistory(sid);
        });
      })
      .catch(() => {
        // Also clean up on error, keep input available; card closing is decided by other processes
        if (activeHitlController === controller) activeHitlController = null;
        activeAgentController.value = null;
        // HITL resume failed: ongoing tools did not complete normally, marked as failed (red ✗)
        chunks.markRunningToolsFailed();
        // Retain draft: cache the completed stages before failure
        void drafts.writeDraftTurn(sid, turnNum);
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
   * @param sid
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

  /** Abort the in-flight HITL resume stream, if any (its abort sends `{type:'stop'}` to the backend). */
  const abortResume = () => {
    activeHitlController?.abort();
    activeHitlController = null;
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
    abortResume,
    clearRequest
  };
}
