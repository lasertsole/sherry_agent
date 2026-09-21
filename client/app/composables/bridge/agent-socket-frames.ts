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
      ctx.markAllReceivedChunk();
      ctx.handlers.onChunk?.(
        typeof data.content === 'string' ? data.content : '',
        data.type ?? 'text',
        data.session_id ?? ctx.sessionId,
        {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error
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
    turn_started: data => {
      const turn = { turnId: data.turn_id ?? '', msgIds: data.message_ids ?? [] };
      ctx.setActiveTurn(turn);
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
        outputTokens: data.output_tokens ?? undefined
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
    todo_updated: data => emit('ws:todo_updated', data)
  });
  try {
    handler(event);
  } catch {
    // Non-JSON frame: ignore.
  }
}
