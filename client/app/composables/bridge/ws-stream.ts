/**
 * Browser-mode agent chat streaming over the backend WebSocket
 * (`/sessions/agent/ws`).
 *
 * @module bridge/wsStream
 */
import type {
  AgentWsEvent,
  ChatRequest,
  HitlInterruptData,
  HitlResponse,
  OnChunkCallback,
  OnDoneCallback,
  OnHitlCallback,
  OnQueuedCallback,
  StreamController
} from './chat-types';
import { KIND_LABEL, uploadBase64ToUrls, type UploadMediaKind } from './upload';
import { teardownWebSocket } from './transport';
import { emit } from '../mitt';
import { API_BASE_URL, WS_BASE_URL } from '../env';
import { StreamInterruptedError, WS_RECONNECT_MAX_ATTEMPTS, wsReconnectDelayMs } from './chat-types';
import { createWsMessageHandler } from '../ws-message';

/**
 * Browser mode: stream agent chat over the backend WebSocket
 * (`/sessions/agent/ws`) instead of the (non-existent) SSE HTTP endpoint.
 *
 * Protocol (see `server/trigger/ws/messages.py`):
 * - The client first uploads base64 images via HTTP POST /images/upload to get
 *   URLs, then sends the message body over the WebSocket:
 *   `{ session_id, multi_modal_message: { text, image_base64_list: [], image_path_list: [uploaded URLs...] } }`
 * - The server returns `{ event: "chunk", content }` streaming frames,
 *   ending with `{ event: "done" }` (success) or `{ event: "error", content }`.
 *
 * @param request  The chat payload.
 * @param onChunk  Called with each text fragment.
 * @param onHitl
 * @param onDone
 * @param onQueued
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
  const baseURL = API_BASE_URL;
  const url = `${WS_BASE_URL}/sessions/agent/ws`;
  const sessionId = request.session_id || 'default';

  let socket: WebSocket | null = null;
  let done: boolean = false;
  /** Whether a chunk has been received — distinguishes Case A (disconnect before the first chunk) from Case B (mid-stream disconnect). */
  let receivedChunk: boolean = false;
  /** Number of connection/reconnect attempts initiated so far (first connect 0, reconnects 1..WS_RECONNECT_MAX_ATTEMPTS). */
  let attempt: number = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Whether a reconnect has been scheduled — on browser failure both onerror and onclose fire in sequence; this flag dedupes them so one disconnect does not consume two attempts from the budget. */
  let reconnectScheduled: boolean = false;
  let release: (err?: unknown) => void = () => {};

  // Single helper that assembles the chat payload: the first connection and the
  // Case A reconnect share the same payload, guaranteeing no fields are lost
  // when the WebSocket is rebuilt.
  const buildChatPayload = (imageUrls: string[], audioUrls: string[], videoUrls: string[]) =>
    JSON.stringify({
      session_id: sessionId,
      multi_modal_message: {
        text: request.text || '',
        image_base64_list: [],
        image_path_list: imageUrls,
        audio_bytes_list: [],
        audio_path_list: audioUrls,
        video_bytes_list: [],
        video_path_list: videoUrls
      }
    });

  const clearReconnectTimer = () => {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

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
      clearReconnectTimer();
      if (socket) {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
        }
        closeSocket();
      }
      release('aborted');
    },
    sendHitlResponse: (response: HitlResponse) => {
      // Silently ignore when the connection is closed/aborted
      if (done || !socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(
        JSON.stringify({
          type: 'hitl_response',
          session_id: sessionId,
          decision: response.decision,
          message: response.message ?? '',
          edited_args: response.edited_args
        })
      );
    }
  };

  const runStream = async (resolve: (value: void | PromiseLike<void>) => void, reject: (reason?: unknown) => void) => {
    release = (err?: unknown) => {
      // After a user-initiated abort the promise stays pending (matches existing behavior)
      if (done && err === 'aborted') return;
      if (err) reject(err instanceof Error ? err : new Error(String(err)));
      else resolve();
    };

    // Upload one media kind to the backend upload endpoint to get HTTP URLs.
    // On failure: finish the stream with the upload error (unless already done)
    // and resolve to `null` so the caller skips the WebSocket entirely.
    const uploadMediaList = async (kind: UploadMediaKind, list: string[]): Promise<string[] | null> => {
      try {
        return await uploadBase64ToUrls(kind, list, baseURL);
      } catch (e) {
        if (!done) {
          done = true;
          release(`${KIND_LABEL[kind]} upload failed: ${e}`);
        }
        return null;
      }
    };

    // Empty kinds resolve synchronously (no await) so the WebSocket is still
    // opened synchronously when there is nothing to upload.
    const imageList = request.image_base64_list;
    const imageUrls = imageList && imageList.length > 0 ? await uploadMediaList('image', imageList) : [];
    if (imageUrls === null) return;
    const audioList = request.audio_bytes_list;
    const audioUrls = audioList && audioList.length > 0 ? await uploadMediaList('audio', audioList) : [];
    if (audioUrls === null) return;
    const videoList = request.video_bytes_list;
    const videoUrls = videoList && videoList.length > 0 ? await uploadMediaList('video', videoList) : [];
    if (videoUrls === null) return;

    // Aborted during upload; do not establish the WebSocket
    if (done) return;

    // Unified handling for connection loss (onerror/onclose firing before done).
    // - Case B (chunk already received): this round's content is already on
    //   screen. The backend cancels the session's active task on every new
    //   connection (`server/trigger/ws/messages.py:171-183`) — re-sending now
    //   would lose the tail that already streamed out, so **never re-send**;
    //   abort immediately and throw StreamInterruptedError, letting the UI
    //   trigger a history reconciliation fallback.
    // - Case A (no chunk received yet): the backend persists the round's
    //   messages only when the agent graph completes, and a new connection
    //   cancels the not-yet-started task, so it is safe to reconnect and
    //   re-send the same payload (exponential backoff, at most
    //   WS_RECONNECT_MAX_ATTEMPTS times).
    const handleConnectionLoss = () => {
      // reconnectScheduled dedup: onclose always follows onerror; handle them only once
      if (done || reconnectScheduled) return;
      clearReconnectTimer();
      if (receivedChunk) {
        // Case B
        done = true;
        closeSocket();
        emit('ws:conn-loss', { sessionId, midStream: true });
        release(new StreamInterruptedError('WebSocket closed after streaming began', true));
        return;
      }
      // Case A
      if (attempt >= WS_RECONNECT_MAX_ATTEMPTS) {
        done = true;
        closeSocket();
        emit('ws:conn-loss', { sessionId, midStream: false });
        emit('stream:reconnect:failed', { sessionId });
        release(new StreamInterruptedError('WebSocket connection error', false));
        return;
      }
      attempt += 1;
      emit('ws:conn-loss', { sessionId, midStream: false });
      emit('stream:reconnecting', { sessionId, attempt, maxAttempts: WS_RECONNECT_MAX_ATTEMPTS });
      reconnectScheduled = true;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        reconnectScheduled = false;
        if (done) return;
        connect();
      }, wsReconnectDelayMs(attempt));
    };

    const connect = () => {
      if (done) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        if (!done) handleConnectionLoss();
        return;
      }
      socket = ws;

      ws.onopen = () => {
        if (done) {
          ws.close();
          return;
        }
        // Reconnect succeeded (not the first connect): notify the UI to collapse the "reconnecting" banner
        if (attempt > 0) emit('stream:reconnected', { sessionId });
        ws.send(buildChatPayload(imageUrls, audioUrls, videoUrls));
      };

      const handleAgentFrame = createWsMessageHandler<AgentWsEvent>({
        chunk: data => {
          receivedChunk = true;
          onChunk(data.content ?? '', data.type ?? 'text', data.session_id ?? sessionId, {
            tool_id: data.tool_id,
            tool_name: data.tool_name,
            args: data.args,
            error: data.error
          });
        },
        hitl_request: data => {
          // HITL interrupt: the agent needs human approval; invoke the onHitl callback (silently ignored when absent)
          if (onHitl && data.content) {
            onHitl(data.content as unknown as HitlInterruptData);
          }
        },
        queued: data => {
          // Session busy: the backend enqueued this message and will stream it later;
          // surface the queue position to the UI badge (silently ignored when absent).
          if (onQueued && typeof data.position === 'number') {
            onQueued({
              sessionId: data.session_id ?? sessionId,
              position: data.position,
              queueSize: data.queue_size ?? 0,
              messageId: data.message_id ?? undefined
            });
          }
        },
        // The agent WS is registered under the running session, so TodoService's
        // live `todo_updated` push arrives here; re-broadcast it for useTodoList.
        todo_updated: data => emit('ws:todo_updated', data),
        done: data => {
          if (!done) {
            done = true;
            clearReconnectTimer();
            closeSocket();
            release();
            // Notify the stream-end callback with model metadata (model_name/input_tokens/output_tokens)
            onDone?.({
              modelName: data.model_name ?? undefined,
              inputTokens: data.input_tokens ?? undefined,
              outputTokens: data.output_tokens ?? undefined
            });
          }
        },
        error: data => {
          if (!done) {
            done = true;
            clearReconnectTimer();
            closeSocket();
            release(data.content || 'WebSocket stream error');
          }
        }
      });

      ws.onmessage = event => {
        handleAgentFrame(event);
      };

      ws.onerror = () => handleConnectionLoss();
      ws.onclose = () => handleConnectionLoss();
    };

    connect();
  };

  const promise = new Promise<void>((resolve, reject) => {
    void runStream(resolve, reject);
  });

  return { controller, promise };
}
