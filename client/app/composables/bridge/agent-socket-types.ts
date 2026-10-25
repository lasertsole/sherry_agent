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

/** Ack payload for the queue-management frames (`queued_cancelled` / `queued_updated` / `send_now_ack`). */
export interface QueuedAckInfo {
  /** Session the queued message belongs to. */
  sessionId: string;
  /** Client `msg_id` of the queued message the frame was about. */
  msgId: string;
  /** Whether the backend applied the change; `false` means the row was no longer QUEUED. */
  ok: boolean;
}

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
  /** `queued_cancelled` -> the queued message was voided (or the cancel failed). */
  onQueuedCancelled?: (info: QueuedAckInfo) => void;
  /** `queued_updated` -> the queued message's text was replaced (or the edit failed). */
  onQueuedUpdated?: (info: QueuedAckInfo) => void;
  /** `send_now_ack` -> the queued message was moved to the front (or it could not be). */
  onSendNowAck?: (info: QueuedAckInfo) => void;
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
  /**
   * Hold inbound frames (the HTTP history+snapshot request is in flight).
   * Frames queue locally instead of reaching the page; release them once the
   * snapshot has been applied so a stale delta cannot land before the
   * authoritative text.
   */
  holdFrames(): void;
  /** Apply the snapshot watermark, then flush every held frame in order. */
  releaseFrames(): void;
  /** Highest frame seq covered by the snapshot, scoped to the snapshot's turn. */
  setSnapshotWatermark(seq: number, turnId?: string): void;
  /** Whether any send is still awaiting its terminal frame. */
  hasPendingSends(): boolean;
  /** Whether any pending send already received a chunk (a running turn, not a resend). */
  hasPendingMidStreamSends(): boolean;
  /** Resolve every pending send without a terminal frame (snapshot said idle). */
  settlePendingSends(): void;
  /** Resolve only the mid-stream sends (a pre-chunk send is resumable). */
  settlePendingMidStreamSends(): void;
  /** Void a queued (not yet delivered) message. */
  sendCancelQueued(msgId: string): void;
  /** Replace the text of a queued message. */
  sendEditQueued(msgId: string, message: string): void;
  /** Interrupt the running turn and deliver this queued message next. */
  sendNow(msgId: string): void;
  /** Tear the connection down and settle every pending send (page unmount). */
  dispose(): void;
}
