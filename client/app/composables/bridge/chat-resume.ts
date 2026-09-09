/**
 * Resume a paused HITL agent over a dedicated WebSocket connection.
 *
 * @module bridge/chatResume
 */
import type { AgentWsEvent, HitlInterruptData, OnChunkCallback, OnHitlCallback, StreamController } from './chat-types';
import { teardownWebSocket } from './transport';
import { WS_BASE_URL } from '../env';
import { createWsMessageHandler } from '../ws-message';

/**
 * Resume a paused HITL agent over a fresh WebSocket, independent of any live
 * streaming socket or AbortController.
 *
 * Unlike `sendHitlResponse` (which is only attached to an in-flight stream
 * returned by `postAgentStream`), this opens a brand-new connection to
 * `/sessions/agent/ws` and sends a `hitl_response` frame. The backend handles
 * that frame before any message validation, so it resumes the paused agent
 * (`Command(resume=...)`) even after a session switch, page refresh, browser
 * restart, or server restart re-persisted the interrupt.
 *
 * Chunks streamed during the resumed execution are routed through `onChunk`
 * (same callback contract as `sendChatMessage`), so the ongoing output still
 * appears in the chat UI. A subsequent `hitl_request` (sequential HITL) is
 * forwarded to `onHitl` so the caller can re-show the approval card.
 *
 * @param sessionId  Session whose agent is paused at the HITL interrupt.
 * @param decision   `"approve"` | `"reject"` | `"edit"`.
 * @param message    Optional message accompanying the decision.
 * @param onChunk    Streamed chunk callback (text/tool_start/tool_end).
 * @param onHitl     Forwarded when the resumed run pauses again for approval.
 * @returns          `{ controller, promise }` — `promise` resolves on `done`,
 *                   rejects on `error`/teardown. `controller` can stop the run.
 */
export function resumeHitl(
  sessionId: string,
  decision: string,
  message: string = '',
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  const url = `${WS_BASE_URL}/sessions/agent/ws`;

  let socket: WebSocket | null = null;
  let done: boolean = false;
  let release: (err?: string) => void = () => {};

  const closeSocket = () => {
    const s = socket;
    socket = null;
    teardownWebSocket(s);
  };

  const controller: StreamController = {
    get closed() {
      return done;
    },
    abort: () => {
      if (done) return;
      done = true;
      if (socket) {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
        }
        closeSocket();
      }
      release('aborted');
    }
  };

  const promise = new Promise<void>((resolve, reject) => {
    release = (err?: string) => {
      if (done && err === 'aborted') return;
      if (err) reject(new Error(err));
      else resolve();
    };

    try {
      socket = new WebSocket(url);
    } catch (e) {
      done = true;
      release(String(e));
      return;
    }

    socket.onopen = () => {
      socket?.send(
        JSON.stringify({
          type: 'hitl_response',
          session_id: sessionId,
          decision,
          message: message ?? ''
        })
      );
    };

    const finishResume = () => {
      if (!done) {
        done = true;
        closeSocket();
        release();
      }
    };

    const handleResumeFrame = createWsMessageHandler<AgentWsEvent>({
      chunk: data => {
        onChunk(data.content ?? '', data.type ?? 'text', data.session_id ?? sessionId, {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error
        });
      },
      hitl_request: data => {
        // Sequential HITL: the resumed execution paused again; forward to the caller to re-show the approval card
        if (onHitl && data.content) {
          onHitl(data.content as unknown as HitlInterruptData);
        }
      },
      done: () => finishResume(),
      stopped: () => finishResume(),
      error: data => {
        if (!done) {
          done = true;
          closeSocket();
          release(data.content || 'WebSocket resume error');
        }
      }
    });

    socket.onmessage = event => {
      handleResumeFrame(event);
    };

    socket.onerror = () => {
      if (!done) {
        done = true;
        closeSocket();
        release('WebSocket resume connection error');
      }
    };

    socket.onclose = () => {
      if (!done) {
        done = true;
        release('WebSocket resume closed before completion');
      }
    };
  });

  return { controller, promise };
}
