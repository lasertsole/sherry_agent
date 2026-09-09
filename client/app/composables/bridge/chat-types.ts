/**
 * Chat protocol types and constants shared across the bridge chat modules.
 *
 * @module bridge/chatTypes
 */

/**
 * Chat request body — used by sendChatMessage / handleSend.
 * Fields mirror the backend `type/message.py` MultiModalMessage.
 */
export interface ChatRequest {
  /** Session ID (when omitted the backend treats it as the "default" session) */
  session_id?: string;
  /** Text content */
  text: string;
  /** Image base64 list (Tauri mode; in browser mode these are uploaded automatically and converted to image_path_list) */
  image_base64_list?: string[];
  /** Image URL list (browser mode; HTTP URLs returned after uploading via /images/upload) */
  image_path_list?: string[];
  /** Audio base64 list (Tauri mode; in browser mode these are uploaded automatically and converted to audio_path_list) */
  audio_bytes_list?: string[];
  /** Audio URL list (browser mode; HTTP URLs returned after uploading via /audio/upload) */
  audio_path_list?: string[];
  /** Video base64 list (Tauri mode; in browser mode these are uploaded automatically and converted to video_path_list) */
  video_bytes_list?: string[];
  /** Video URL list (browser mode; HTTP URLs returned after uploading via /video/upload) */
  video_path_list?: string[];
}

/**
 * Streaming event frames returned by the backend `/sessions/agent/ws` in browser mode.
 * Corresponds to `{"event": ..., "session_id": ..., "content": ...}` in `server/trigger/ws/messages.py`.
 */
export type AgentWsEventType = 'chunk' | 'done' | 'error' | 'stopped' | 'hitl_request' | 'queued';

/** Chunk type — distinguishes conversational text from tool-call markers. */
export type AgentChunkType = 'text' | 'reasoning' | 'tool_start' | 'tool_end' | 'tool_result';

/** HITL interrupt payload sent by the server when the agent pauses for human approval. */
export interface HitlInterruptData {
  tool_name: string;
  tool_args: Record<string, unknown>;
  description: string;
  allowed_decisions: string[];
}

/** HITL decision sent by the client to resume the agent. */
export interface HitlResponse {
  decision: 'approve' | 'reject' | 'edit';
  message?: string;
  edited_args?: Record<string, unknown>;
}

export interface AgentWsEvent {
  event: AgentWsEventType;
  session_id?: string | null;
  content?: string;
  /** Chunk type (only present on "chunk" events). Defaults to "text" for backwards compat. */
  type?: AgentChunkType;
  /** Tool-call metadata (only present on "tool_result" chunks). */
  tool_id?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
  error?: boolean;
  /** Model name (carried only on done frames; from the backend model_name) */
  model_name?: string;
  /** Input token count (carried only on done frames; from the backend input_tokens) */
  input_tokens?: number;
  /** Output token count (carried only on done frames; from the backend output_tokens) */
  output_tokens?: number;
  /** 1-based position of this message in the session's input queue (carried only on queued frames). */
  position?: number;
  /** Current queue depth, this message included (carried only on queued frames). */
  queue_size?: number;
  /** Server-assigned id of the enqueued message (carried only on queued frames). */
  message_id?: string;
}

/**
 * Error used to reject when the stream is interrupted by the network
 * (WebSocket reconnect retries exhausted).
 * `midStream` being true means the disconnect happened **after the first chunk
 * of this round had already been produced** — any content is already on screen
 * and must never be re-sent (see the handleSend Case B comment); the UI can
 * only mark the round as failed and trigger a history reconciliation fallback.
 */
export class StreamInterruptedError extends Error {
  constructor(
    message: string,
    /** true = at least one chunk was received before the disconnect (this round's content is already on screen); false = pure connection failure before the first chunk */
    readonly midStream: boolean
  ) {
    super(message);
    this.name = 'StreamInterruptedError';
  }
}

/**
 * Maximum number of reconnect attempts for a browser-mode WebSocket stream loss.
 * After each failure it retries with exponential backoff (1000 * 2^(attempt-1) ms);
 * once this cap is exceeded, {@link StreamInterruptedError} is thrown.
 * The backend cancels the session's active task on every new connection and only
 * persists the round when the agent graph completes, so reconnecting and
 * re-sending "before the first chunk" is safe
 * (see `server/trigger/ws/messages.py:171-183`).
 */
export const WS_RECONNECT_MAX_ATTEMPTS = 3;

/**
 * Exponential backoff: wait time in milliseconds before the `attempt`-th (1-based) reconnect.
 * @param attempt
 * @example wsReconnectDelayMs(1) === 1000; wsReconnectDelayMs(2) === 2000; wsReconnectDelayMs(3) === 4000
 */
export function wsReconnectDelayMs(attempt: number): number {
  return 1000 * 2 ** (attempt - 1);
}

/** mitt event name — WebSocket stream connection loss (stream drop). Payload is the session ID of the interrupted stream. */
export const WS_CONN_LOSS_EVENT = 'ws:conn-loss';

/**
 * Typed chunk callback: receives the text fragment, its semantic type, the
 * session id the chunk belongs to, and optional tool-call metadata (present
 * only on `tool_result` chunks). The session id lets the caller route chunks
 * to the correct per-session ChatPage when multiple sessions stream concurrently.
 */
export type OnChunkCallback = (
  content: string,
  type: AgentChunkType,
  sessionId: string,
  meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
) => void;

/** HITL interrupt callback: invoked when the agent pauses for human approval. */
export type OnHitlCallback = (data: HitlInterruptData) => void;

/**
 * Queued-notification payload: the backend accepted the message but the session
 * is busy, so the message was enqueued and will stream once earlier turns finish
 * (contract: `{"event":"queued","session_id":"...","position":N,"queue_size":M,"message_id":"..."}`).
 */
export interface QueuedInfo {
  /** Session the queued message belongs to (lets KeepAlive-cached pages filter against their frozen sid). */
  sessionId: string;
  /** 1-based position of this message in the session's input queue. */
  position: number;
  /** Current queue depth, this message included. */
  queueSize: number;
  /** Server-assigned id of the enqueued message (optional passthrough). */
  messageId?: string;
}

/** Queued callback: invoked when the backend reports a `queued` frame instead of streaming immediately. */
export type OnQueuedCallback = (info: QueuedInfo) => void;

/** Stream-end callback: carries optional model metadata (model_name/input_tokens/output_tokens, from the done frame). */
export type OnDoneCallback = (meta?: { modelName?: string; inputTokens?: number; outputTokens?: number }) => void;

/**
 * A handle that can be used to stop an ongoing generation request.
 *
 * `abort()` instructs the backend to halt the stream for the session and
 * tears down the underlying WebSocket. `closed` becomes `true` once torn down.
 */
export interface StreamController {
  /** Whether the connection has been closed/stopped/errored. */
  readonly closed: boolean;
  /** Stop the generation and close the connection. */
  abort(): void;
  /** Send a HITL decision (approve/reject/edit) to resume a pending agent. */
  sendHitlResponse?(response: HitlResponse): void;
}
