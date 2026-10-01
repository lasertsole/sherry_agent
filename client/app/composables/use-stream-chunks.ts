/**
 * Streamed-chunk rendering slice: merges chunks of one semantic type into the
 * session page's message list and marks unfinished tool cards as failed.
 */
import type { Ref } from 'vue';
import type { AgentChunkType } from './bridge';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '../pages/home/type';
import type { DraftPersistence } from './use-draft-persistence';

/** Tool-call metadata carried by tool_result chunks. */
export interface StreamChunkMeta {
  tool_id?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
  error?: boolean;
}

/** Per-chunk-type context shared by every CHUNK_HANDLERS entry. */
interface ChunkHandlerContext {
  /** Session id */
  sid: string;
  /** Chunk body (the text for text/reasoning, the tool name for tool_start, the result text for tool_result) */
  content: string;
  /** This turn's turn number (new messages are written into this turn) */
  turnNum: number;
  /** Whether the chunk belongs to an "active draft turn" (created on send, removed after onDone/error/stop) */
  isActiveDraft: boolean;
  /** Tool call metadata (only present for tool_result) */
  meta?: StreamChunkMeta;
}

/**
 * Create the streamed-chunk rendering slice for one session page.
 *
 * @param chatMessages The page's message list (single source of truth)
 * @param allocateTempId Temporary id allocator (large negative, creation-ordered)
 * @param drafts Draft persistence slice (chunk handlers persist at branch-specific cadence)
 */
export function useStreamChunks(
  chatMessages: Ref<MessageItem[]>,
  allocateTempId: () => number,
  drafts: Pick<DraftPersistence, 'scheduleDraftWrite' | 'commitDraftTurn' | 'isDraftTurnActive'>
) {
  /**
   * Index of the LAST row belonging to `turnNum`, or `-1` when the turn has none.
   *
   * A turn's rows are not always at the array tail: a send issued while the
   * session is busy appends its optimistic bubbles immediately (they appear in
   * the queue), so a LATER turn's rows can sit after this turn's own. Every
   * streamed write must therefore resolve its target BY TURN — a "the tail is
   * my row" check silently misfires and scatters one turn's rows (live-verified:
   * text and token meta ended up on different rows of the same turn).
   * @param turnNum
   */
  const lastSameTurnIdx = (turnNum: number): number => {
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      if (chatMessages.value[i]?.turn_num === turnNum) return i;
    }
    return -1;
  };

  /**
   * The turn's trailing AI row — the row that receives this turn's streamed
   * content (text / reasoning), or `undefined` when a new one must be created.
   * @param turnNum
   */
  const sameTurnTailAi = (turnNum: number): MessageItem | undefined => {
    const idx = lastSameTurnIdx(turnNum);
    const row = idx >= 0 ? chatMessages.value[idx] : undefined;
    return row && row.role === CHAT_ROLE.AI ? row : undefined;
  };

  /**
   * Create a new AI message of the given turn and place it right after that
   * turn's last row (appending at the array end would separate it from its own
   * turn when a later turn's bubbles already sit there).
   * @param sid
   * @param turnNum
   */
  const pushAiMessage = (sid: string, turnNum: number): MessageItem => {
    const msg: MessageItem = {
      session_id: sid,
      role: CHAT_ROLE.AI,
      content: '',
      reasoning: '',
      id: allocateTempId(),
      turn_num: turnNum,
      timestamp: new Date().toISOString()
    };
    const idx = lastSameTurnIdx(turnNum);
    if (idx >= 0) chatMessages.value.splice(idx + 1, 0, msg);
    else chatMessages.value.push(msg);
    return msg;
  };

  /**
   * Find the index of the most recent TOOL message of the given turn.
   * @param turnNum
   */
  const findSameTurnToolIdx = (turnNum: number): number => {
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      const row = chatMessages.value[i];
      if (!row) continue;
      if (row.role === CHAT_ROLE.TOOL && row.turn_num === turnNum) return i;
    }
    return -1;
  };

  /**
   * Strategy registry for streamed chunk types (`CHUNK_HANDLERS[chunk.type]`).
   *
   * Each handler merges one streamed chunk into `chatMessages` (the single source
   * of truth) and persists the draft at the same cadence as the original branches:
   * text/reasoning share the debounced append path, discrete tool stages are
   * committed immediately. The registry is exhaustive over `AgentChunkType`.
   */
  const CHUNK_HANDLERS: Record<AgentChunkType, (ctx: ChunkHandlerContext) => void> = {
    text: ({ sid, content, turnNum, isActiveDraft }) => {
      const tail = sameTurnTailAi(turnNum);
      if (tail) {
        // Tail of same turn is AI → append the body
        tail.content += content;
      } else {
        // Tail is TOOL / not this turn → create a new AI message to carry it
        pushAiMessage(sid, turnNum).content = content;
      }
      if (isActiveDraft) drafts.scheduleDraftWrite(sid, turnNum);
    },
    reasoning: ({ sid, content, turnNum, isActiveDraft }) => {
      // Model thinking block: appended chunk by chunk into the `reasoning` field of the same-turn tail AI message,
      // without interfering with body text accumulation.
      // When the tail is TOOL / not this turn, create a new AI placeholder message to carry it (the body may arrive later).
      const target = sameTurnTailAi(turnNum) ?? pushAiMessage(sid, turnNum);
      target.reasoning = (target.reasoning ?? '') + content;
      // Thinking blocks are discrete stages; debouncing seems intuitive, but thinking content must be persisted in real time
      // with the stream to support refresh recovery, so it simply shares the text-append debounce path
      // (thinking blocks are usually not subdivided as frequently as body text).
      if (isActiveDraft) drafts.scheduleDraftWrite(sid, turnNum);
    },
    tool_start: ({ sid, content, turnNum, isActiveDraft, meta }) => {
      chatMessages.value.push({
        session_id: sid,
        role: CHAT_ROLE.TOOL,
        content: '',
        toolName: content,
        toolStatus: 'running',
        // Args are delivered with meta at tool_start time, so call arguments can be viewed while running
        toolArgs: meta?.args ?? undefined,
        id: allocateTempId(),
        turn_num: turnNum,
        timestamp: new Date().toISOString()
      });
      if (isActiveDraft) void drafts.commitDraftTurn(sid, turnNum);
    },
    tool_end: ({ sid, turnNum, isActiveDraft }) => {
      // Mark the most recent TOOL message of this turn as completed
      const targetIdx = findSameTurnToolIdx(turnNum);
      if (targetIdx >= 0) {
        const row = chatMessages.value[targetIdx];
        if (row) row.toolStatus = 'done';
      }
      if (isActiveDraft) void drafts.commitDraftTurn(sid, turnNum);
    },
    tool_result: ({ sid, content, turnNum, isActiveDraft, meta }) => {
      // Fill the most recent same-turn TOOL message with args and result text, and mark its status per `error`.
      // HITL resume special case: the interrupted tool card was created in the PREVIOUS (generate) turn,
      // while its tool_result arrives with the resume turn number (max+1) — the same-turn search misses it.
      // Fallback: the most recent TOOL card still 'running' (approve path) or 'failed' (reject path —
      // markRunningToolsFailed already ran before the resume frames arrive), so the execution result /
      // rejection notice lands on the card the user actually saw.
      let targetIdx = findSameTurnToolIdx(turnNum);
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
      if (isActiveDraft) void drafts.commitDraftTurn(sid, turnNum);
    }
  };

  /**
   * Merge one streamed chunk into `chatMessages` by dispatching to `CHUNK_HANDLERS[type]`.
   *
   * This function is the shared message-rendering logic used by both `handleSend` (normal chat)
   * and the "HITL resume" path. The `turnNum` parameter bounds the scope, so unrelated history
   * messages are never mistaken for the target of the current streaming turn.
   *
   * @param sid Session id
   * @param content Chunk text (the body for text, the tool name for tool_start, the result text for tool_result)
   * @param type Semantic type
   * @param turnNum This turn's turn number (new messages are written into this turn)
   * @param meta Tool call metadata (only present for tool_result)
   */
  const appendStreamChunk = (
    sid: string,
    content: string,
    type: AgentChunkType,
    turnNum: number,
    meta?: StreamChunkMeta
  ) => {
    // Determine whether this belongs to an "active draft turn" (created on send, removed after onDone/error/stop).
    // On hit, write the draft in layers at the tail: text appends are debounced 200ms, discrete tool stages / first text are written immediately.
    const isActiveDraft = drafts.isDraftTurnActive(turnNum);
    CHUNK_HANDLERS[type]({ sid, content, turnNum, isActiveDraft, meta });
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

  return { appendStreamChunk, markRunningToolsFailed };
}
