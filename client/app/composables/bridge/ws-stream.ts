/**
 * Browser-mode agent chat streaming over the shared per-session WebSocket
 * (`/sessions/agent/ws`).
 *
 * The transport itself lives in `./agent-socket`: exactly one persistent socket
 * per session, reused by every send / stop / HITL response. This module keeps
 * the historical `sendChatMessageWs` entry point, merging the per-call
 * callbacks into the session-level handlers and delegating the actual send.
 *
 * @module bridge/wsStream
 */
import type {
  ChatRequest,
  OnChunkCallback,
  OnDoneCallback,
  OnHitlCallback,
  OnQueuedCallback,
  StreamController
} from './chat-types';
import { acquireAgentSocket } from './agent-socket';

/**
 * Browser mode: stream agent chat over the shared per-session WebSocket
 * (`/sessions/agent/ws`).
 *
 * Protocol (frozen):
 * - Send: `{ session_id, msg_id, multi_modal_message: {...} }` (`msg_id` required).
 * - Stop / HITL response: `{ type: "stop" | "hitl_response", session_id, ... }`
 *   sent on the SAME socket without closing it.
 * - Server frames: `turn_started`, `chunk`, `queued`, `done`, `error`, `stopped`,
 *   `hitl_request`, `todo_updated`.
 *
 * @param request  The chat payload (must carry a `msg_id`).
 * @param onChunk  Session-level chunk callback (one invocation per `chunk` frame).
 * @param onHitl   Session-level HITL callback.
 * @param onDone   Turn-completion callback (carries model metadata).
 * @param onQueued Session-level queued callback (queue badge).
 * @returns        `{ controller, promise }` — `promise` resolves on completion.
 */
export function sendChatMessageWs(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
  onDone?: OnDoneCallback,
  onQueued?: OnQueuedCallback
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  const sessionId = request.session_id || 'default';
  const socket = acquireAgentSocket(sessionId);
  socket.setHandlers({ onChunk, onHitl, onDone, onQueued });
  return socket.send(request);
}
