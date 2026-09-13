/**
 * Draft persistence for in-flight agent turns (IndexedDB-backed, per session page).
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
import type { Ref } from 'vue';
import type { MessageItem } from '../pages/home/type';

/** Draft persistence API shared by the chat stream, HITL resume and lifecycle paths. */
export interface DraftPersistence {
  /** Allocate the next local temporary message id (large negative, incrementing so ascending id order equals creation order within a turn). */
  allocateTempId: () => number;
  /** Snapshot of the turn numbers currently streaming (real turn_num values). */
  activeTurns: () => number[];
  /** Persist all messages of the given turn from `chatMessages` as one local draft. */
  writeDraftTurn: (sid: string, turnNum: number) => Promise<void>;
  /** Schedule one "text append" draft write (200ms trailing debounce). */
  scheduleDraftWrite: (sid: string, turnNum: number) => void;
  /** Write the draft immediately (discrete stages) and cancel the turn's pending text debounce. */
  commitDraftTurn: (sid: string, turnNum: number) => Promise<void>;
  /** Clear a turn's draft and cancel its pending debounce timer. */
  removeDraftTurn: (sid: string, turnNum: number) => void;
  /** Register one active draft turn (created on send, removed on completion). */
  trackDraftTurn: (sid: string, turnNum: number) => void;
  /** Determine whether a turn is in the active draft-persisting state. */
  isDraftTurnActive: (turnNum: number) => boolean;
  /** Remove a turn's draft registration after the server successfully persists. */
  untrackDraftTurn: (sid: string, turnNum: number) => void;
}

/**
 * Create the draft persistence slice for one session page.
 * @param chatMessages The page's message list (single source of truth; draft rows are snapshotted from it)
 */
export function useDraftPersistence(chatMessages: Ref<MessageItem[]>): DraftPersistence {
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

  /** 200ms debounce timer for text-append draft writes (key: `${sid}:${turnNum}`) */
  const draftDebounceTimers = new Map<string, ReturnType<typeof setTimeout>>();

  /**
   * Set of "active turns" currently streaming (elements are real turn_num values).
   *
   * `handleSend` and the "HITL resume" path each push one turn_num when starting a stream; it is removed when the
   * stream ends normally / errors / is aborted / is rejected. `appendStreamChunk` writes drafts only when turn_num
   * belongs to this set, so history rows never trigger accidental disk writes.
   */
  const activeDraftTurns = new Set<number>();

  const allocateTempId = (): number => tempIdCounter++;

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
    try {
      await saveDraftTurn({ session_id: sid, turn_num: turnNum, messages: rows });
    } catch (e) {
      logUtil.w('[writeDraftTurn] 草稿写入失败：', sid, turnNum, e);
    }
  };

  /**
   * Schedule one "text append" draft write (200ms trailing debounce).
   *
   * High-frequency text chunks are not persisted one by one but merged via the debounce; discrete stages
   * (send / each tool stage / error / first text / server completion) are written immediately by callers via `commitDraftTurn`.
   * @param sid
   * @param turnNum
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
   * @param sid
   * @param turnNum
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
   * @param sid
   * @param turnNum
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
   * Register one active draft turn (created on send, removed on completion).
   * @param sid
   * @param turnNum
   */
  const trackDraftTurn = (sid: string, turnNum: number) => {
    activeDraftTurns.add(turnNum);
    // Write one draft at creation time, guaranteeing the fastest "cache-on-send" frame (user message + empty AI placeholder).
    void writeDraftTurn(sid, turnNum);
  };

  const isDraftTurnActive = (turnNum: number): boolean => activeDraftTurns.has(turnNum);

  const activeTurns = (): number[] => [...activeDraftTurns];

  /**
   * Remove a turn's draft registration. Called after the server successfully persists: clear the draft + cancel the
   * pending debounce timer; the caller then triggers `loadSessionHistory` to replace local negative temporary-id rows
   * with the server's positive-id rows.
   * @param sid
   * @param turnNum
   */
  const untrackDraftTurn = (sid: string, turnNum: number) => {
    const had = activeDraftTurns.delete(turnNum);
    removeDraftTurn(sid, turnNum);
    void had; // keep ref for clarity
  };

  return {
    allocateTempId,
    activeTurns,
    writeDraftTurn,
    scheduleDraftWrite,
    commitDraftTurn,
    removeDraftTurn,
    trackDraftTurn,
    isDraftTurnActive,
    untrackDraftTurn
  };
}
