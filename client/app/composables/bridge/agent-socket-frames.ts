/**
 * Inbound frame routing for the per-session agent socket.
 *
 * `routeAgentFrame` maps one `message` event to the session-level handlers and
 * settles the in-flight sends of the addressed turn. It is deliberately state
 * agnostic: the owning class supplies the current handlers, the pending-send
 * registry and the active-turn pointer through {@link AgentFrameContext}.
 *
 * @module bridge/agent-socket-frames
 */
import type { AgentWsEvent } from './chat-types';
import { isHitlInterruptData } from './chat-types';
import { createWsMessageHandler } from '../ws-message';
import { emit } from '../mitt';
import type { AgentSocketHandlers } from './agent-socket-types';

/** Mutable state + sinks of the owning socket, injected per frame. */
export interface AgentFrameContext {
  /** Session the socket belongs to (fallback when a frame omits `session_id`). */
  sessionId: string;
  /** Session-level handlers installed by the page. */
  handlers: AgentSocketHandlers;
  /** Mark every in-flight send as having received a chunk. */
  markAllReceivedChunk(): void;
  /** Settle the given sends as resolved. */
  settleResolve(messageIds: string[]): void;
  /** Settle the given sends as rejected. */
  settleReject(messageIds: string[], err: unknown): void;
  /** Current turn pointer (null when no turn is active). */
  getActiveTurn(): { turnId: string; msgIds: string[] } | null;
  /** Replace the turn pointer. */
  setActiveTurn(turn: { turnId: string; msgIds: string[] } | null): void;
  /** Every in-flight `msg_id` (fallback target of settle). */
  pendingKeys(): string[];
  /** Resolve and clear the pending stop() waiters. */
  flushStopResolvers(): void;
  /** Highest frame seq covered by the last resume snapshot (chunk watermark). */
  getSnapshotSeq(): number;
  /** Record the snapshot's seq (the watermark is scoped to its turn). */
  setSnapshotSeq(seq: number): void;
  /** Turn identity the snapshot seq belongs to ('' when the snapshot omitted it). */
  getSnapshotTurnId(): string;
  /** Record the snapshot's turn identity. */
  setSnapshotTurnId(turnId: string): void;
}

/**
 * Resolve `message_ids`, falling back to the active turn's member ids, then all pending.
 * @param ctx
 * @param messageIds
 */
function turnMessageIds(ctx: AgentFrameContext, messageIds?: string[]): string[] {
  if (messageIds && messageIds.length > 0) return messageIds;
  const activeTurn = ctx.getActiveTurn();
  if (activeTurn && activeTurn.msgIds.length > 0) return activeTurn.msgIds;
  return ctx.pendingKeys();
}

/**
 * Route one raw WebSocket message event (non-JSON frames are ignored).
 * @param event
 * @param ctx
 */
export function routeAgentFrame(event: MessageEvent, ctx: AgentFrameContext): void {
  const handler = createWsMessageHandler<AgentWsEvent>({
    chunk: data => {
      // A resumed viewer drops text produced at or before its snapshot: those
      // deltas are already inside the replacement text. The watermark is
      // per-turn — a NEW turn's seq restarts at 1 and must never be dropped
      // because it is numerically below another turn's snapshot seq.
      const seq = (data as { seq?: unknown }).seq;
      const turnId = data.turn_id ?? '';
      const sameTurn = !turnId || !ctx.getSnapshotTurnId() || turnId === ctx.getSnapshotTurnId();
      if (typeof seq === 'number' && seq <= ctx.getSnapshotSeq() && sameTurn) return;
      ctx.markAllReceivedChunk();
      ctx.handlers.onChunk?.(
        typeof data.content === 'string' ? data.content : '',
        data.type ?? 'text',
        data.session_id ?? ctx.sessionId,
        {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error,
          duration_ms: data.duration_ms,
          turn_id: turnId || undefined
        }
      );
    },
    hitl_request: data => {
      if (ctx.handlers.onHitl && isHitlInterruptData(data.content)) {
        ctx.handlers.onHitl(data.content);
      }
    },
    queued: data => {
      if (ctx.handlers.onQueued && typeof data.position === 'number') {
        ctx.handlers.onQueued({
          sessionId: data.session_id ?? ctx.sessionId,
          position: data.position,
          queueSize: data.queue_size ?? 0,
          messageId: data.message_id ?? undefined
        });
      }
    },
    terminal_bg_started: data => {
      const content = (data.content ?? {}) as Record<string, unknown>;
      if (typeof content.task_id !== 'string' || !content.task_id) return;
      ctx.handlers.onTerminalBgStarted?.({
        task_id: content.task_id,
        command: typeof content.command === 'string' ? content.command : '',
        status: typeof content.status === 'string' ? content.status : 'running',
        pid: typeof content.pid === 'number' ? content.pid : null,
        exit_code: typeof content.exit_code === 'number' ? content.exit_code : null,
        session_id: data.session_id ?? ctx.sessionId,
        output_path: typeof content.output_path === 'string' ? content.output_path : null
      });
    },
    terminal_bg_exit: data => {
      const content = (data.content ?? {}) as Record<string, unknown>;
      if (typeof content.task_id !== 'string' || !content.task_id) return;
      ctx.handlers.onTerminalBgExit?.({
        taskId: content.task_id,
        // Older backends sent no status: an exit frame with a zero code is a
        // completion, anything else a failure — the kill reason arrives only
        // from a backend that reports it.
        status: typeof content.status === 'string' ? content.status : 'completed',
        reason: typeof content.reason === 'string' ? content.reason : 'exited',
        exitCode: typeof content.exit_code === 'number' ? content.exit_code : null
      });
    },
    terminal_bg_kill_ack: data => {
      const content = (data.content ?? {}) as Record<string, unknown>;
      ctx.handlers.onTerminalBgKillAck?.({
        taskId: typeof content.task_id === 'string' ? content.task_id : '',
        ok: content.ok === true,
        reason: typeof content.reason === 'string' ? content.reason : null
      });
    },
    queued_cancelled: data => {
      ctx.handlers.onQueuedCancelled?.({
        sessionId: data.session_id ?? ctx.sessionId,
        msgId: data.msg_id ?? '',
        ok: data.ok === true
      });
    },
    queued_updated: data => {
      ctx.handlers.onQueuedUpdated?.({
        sessionId: data.session_id ?? ctx.sessionId,
        msgId: data.msg_id ?? '',
        ok: data.ok === true
      });
    },
    send_now_ack: data => {
      ctx.handlers.onSendNowAck?.({
        sessionId: data.session_id ?? ctx.sessionId,
        msgId: data.msg_id ?? '',
        ok: data.ok === true
      });
    },
    turn_started: data => {
      const turn = { turnId: data.turn_id ?? '', msgIds: data.message_ids ?? [] };
      ctx.setActiveTurn(turn);
      // Do NOT clear the snapshot watermark here: a new turn's seq restarts at
      // 1 and is kept by the per-turn check above; a queued turn_started for
      // the snapshot's own turn must not make its old deltas replayable.
      ctx.handlers.onTurnStarted?.({
        sessionId: data.session_id ?? ctx.sessionId,
        turnId: turn.turnId,
        messageIds: turn.msgIds
      });
    },
    done: data => {
      ctx.settleResolve(turnMessageIds(ctx, data.message_ids));
      ctx.setActiveTurn(null);
      ctx.handlers.onDone?.({
        modelName: data.model_name ?? undefined,
        inputTokens: data.input_tokens ?? undefined,
        outputTokens: data.output_tokens ?? undefined,
        turnId: data.turn_id ?? undefined
      });
    },
    error: data => {
      const message = typeof data.content === 'string' ? data.content : '';
      ctx.settleReject(turnMessageIds(ctx, data.message_ids), new Error(message || 'WebSocket stream error'));
      ctx.setActiveTurn(null);
    },
    stopped: data => {
      ctx.settleResolve(turnMessageIds(ctx, data.message_ids));
      ctx.setActiveTurn(null);
      ctx.flushStopResolvers();
    },
    stream_resume: data => {
      const content = (data.content ?? {}) as {
        status?: string;
        text?: string;
        has_reasoning?: boolean;
        seq?: number;
        turn_id?: string;
        turn_num?: number;
      };
      const status = content.status ?? 'unknown';
      if (status === 'ok') {
        const snapshotSeq = typeof content.seq === 'number' ? content.seq : 0;
        ctx.setSnapshotSeq(snapshotSeq);
        ctx.setSnapshotTurnId(content.turn_id ?? '');
        ctx.markAllReceivedChunk();
        emit('stream:resumed', {
          sessionId: data.session_id ?? ctx.sessionId,
          status,
          text: typeof content.text === 'string' ? content.text : '',
          hasReasoning: content.has_reasoning === true,
          seq: snapshotSeq,
          turnId: content.turn_id ?? '',
          turnNum: content.turn_num
        });
        return;
      }
      // ended / gone / unknown: there is no running turn to resume — settle the
      // in-flight sends so the UI closes (like `done`), then let the business
      // layer refetch for ended/unknown (gone = nothing to fetch).
      ctx.settleResolve(turnMessageIds(ctx));
      ctx.setActiveTurn(null);
      emit('stream:resumed', { sessionId: data.session_id ?? ctx.sessionId, status });
    },
    todo_updated: data => emit('ws:todo_updated', data),
    load_warning: data => emit('ws:load_warning', data),
    taskflow_updated: data => emit('ws:taskflow_updated', data),
    model_retry: data => emit('ws:model_retry', data),
    model_download_updated: data => emit('ws:model_download_updated', data),
    // Best-effort downgrade notice from `web_search` (the turn keeps streaming);
    // the store renders it as a dismissible banner.
    search_backend_fallback: data => emit('ws:search_backend_fallback', data)
  });
  try {
    handler(event);
  } catch {
    // Non-JSON frame: ignore.
  }
}
