/**
 * Public protocol types of the per-session agent socket.
 *
 * Split out of `agent-socket.ts` so the class file holds only orchestration; the
 * module re-exports these names unchanged for the `bridge.ts` facade.
 *
 * @module bridge/agent-socket-types
 */
import type {
  ChatRequest,
  HitlResponse,
  OnChunkCallback,
  OnDoneCallback,
  OnHitlCallback,
  OnQueuedCallback,
  StreamController
} from './chat-types';

/** Notification payload for a `turn_started` frame. */
export interface TurnStartedInfo {
  /** Session the turn belongs to. */
  sessionId: string;
  /** Server-assigned turn id. */
  turnId: string;
  /** Client `msg_id`s whose sends make up this turn (N user bubbles -> one AI reply). */
  messageIds: string[];
}

/**
 * Session-level handlers set once by the page: every frame of the session is
 * routed here regardless of which `send()` produced it.
 */
export interface AgentSocketHandlers {
  /** One invocation per `chunk` frame. */
  onChunk?: OnChunkCallback;
  /** `hitl_request` -> approval card. */
  onHitl?: OnHitlCallback;
  /** `turn_started` -> consolidate placeholders / clear member badges. */
  onTurnStarted?: (info: TurnStartedInfo) => void;
  /** `queued` -> queue badge for the (busy) session. */
  onQueued?: OnQueuedCallback;
  /** `done` -> turn succeeded (carries model metadata). */
  onDone?: OnDoneCallback;
}

/**
 * The session-scoped socket handle returned by `acquireAgentSocket`.
 */
export interface AgentSocket {
  readonly sessionId: string;
  /** Merge handlers (page sets `onTurnStarted` once; per-send callbacks are merged in). */
  setHandlers(handlers: AgentSocketHandlers): void;
  /** Send a chat request; returns a controller + a promise settled by the turn's end. */
  send(request: ChatRequest): { controller: StreamController; promise: Promise<void> };
  /** Send a stop frame on the SAME socket (never closes it). */
  stop(): Promise<void>;
  /** Send a HITL decision on the SAME socket (never opens a new connection). */
  sendHitlResponse(response: HitlResponse): void;
  /** Tear the connection down and settle every pending send (page unmount). */
  dispose(): void;
}
