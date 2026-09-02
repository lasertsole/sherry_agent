/**
 * Unified communication bridge between the Nuxt frontend and the backend.
 *
 * Supports two runtime modes:
 * - **Tauri desktop**: IPC via `invoke()` + Tauri Events for streaming
 * - **Browser dev**: direct HTTP to the Python backend via `fetchApi()`
 *
 * All API calls go through this module so that components never
 * need to know which transport layer is active.
 *
 * @module bridge
 */

import type { HistoryMessage } from '~/types/backend/HistoryMessage';
import type { PromptFileResponse } from '~/types/backend/PromptFileResponse';
import type { HealthStatus } from '~/types/backend/HealthStatus';
import type { Response as ApiResponse } from '~/types/response';
import { fetchApi } from './requestApi';
import { emit } from './mitt';

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
 * @example wsReconnectDelayMs(1) === 1000; wsReconnectDelayMs(2) === 2000; wsReconnectDelayMs(3) === 4000
 */
export function wsReconnectDelayMs(attempt: number): number {
  return 1000 * 2 ** (attempt - 1);
}

/** mitt event name — WebSocket stream connection loss (stream drop). Payload is the session ID of the interrupted stream. */
export const WS_CONN_LOSS_EVENT = 'ws:conn-loss';

/** Tauri stream-event payloads (mirror `src-tauri/src/commands/events.rs`). */
interface AgentStreamStart {
  session_id: string;
}
interface AgentStreamChunk {
  session_id: string;
  content: string;
  is_final?: boolean;
  /** Chunk type — "text" | "reasoning" | "tool_start" | "tool_end" | "tool_result". Defaults to "text". */
  chunk_type?: AgentChunkType;
  /** Tool-call metadata (only present on "tool_result" chunks). */
  tool_id?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
  error?: boolean;
}

interface AgentStreamEnd {
  session_id: string;
  content: string;
}
interface AgentStreamError {
  session_id: string;
  code: number;
  message: string;
}
interface ChatChunk {
  id: string;
  role: string;
  content: string;
}

// ── Runtime detection ────────────────────────────────────

/**
 * Returns `true` when running inside the Tauri desktop shell.
 */
function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

// ── Lazy Tauri imports ───────────────────────────────────

async function getInvoke() {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke;
}

async function getListen() {
  const { listen } = await import('@tauri-apps/api/event');
  return listen;
}

// ── Agent chat (streaming) ───────────────────────────────

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
 * Send a chat message and receive streaming chunks.
 *
 * In Tauri mode the chunks arrive via Tauri Events
 * (`agent:stream:chunk`). In browser mode the request streams over the
 * backend WebSocket (`/sessions/agent/ws`).
 *
 * @param request  The chat payload (session_id, text, images).
 * @param onChunk  Callback invoked for each text fragment with its type and session id.
 * @returns        Resolves when the stream completes; rejects on error.
 */
export async function sendChatMessage(request: ChatRequest, onChunk: OnChunkCallback): Promise<void> {
  return streamChatMessage(request, onChunk).promise;
}

/** Tauri mode: invoke IPC + listen for Tauri Events. */
async function sendChatMessageTauri(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onDone?: OnDoneCallback
): Promise<void> {
  const invoke = await getInvoke();
  const listen = await getListen();

  return new Promise<void>((resolve, reject) => {
    let unlistenChunk: (() => void) | null = null;
    let unlistenEnd: (() => void) | null = null;
    let unlistenErr: (() => void) | null = null;
    let unlistenStart: (() => void) | null = null;

    const cleanup = () => {
      unlistenChunk?.();
      unlistenEnd?.();
      unlistenErr?.();
      unlistenStart?.();
    };

    // Listen for stream start
    listen<AgentStreamStart>('agent:stream:start', () => {
      // Stream started, no action needed
    }).then(fn => {
      unlistenStart = fn;
    });

    // Listen for chunks
    listen<AgentStreamChunk>('agent:stream:chunk', event => {
      const p = event.payload;
      onChunk(p.content, p.chunk_type ?? 'text', p.session_id, {
        tool_id: p.tool_id,
        tool_name: p.tool_name,
        args: p.args,
        error: p.error
      });
    }).then(fn => {
      unlistenChunk = fn;
    });

    // Listen for stream end
    listen<AgentStreamEnd>('agent:stream:end', () => {
      cleanup();
      onDone?.();
      resolve();
    }).then(fn => {
      unlistenEnd = fn;
    });

    // Listen for stream error
    listen<AgentStreamError>('agent:stream:error', event => {
      cleanup();
      reject(new Error(`[${event.payload.code}] ${event.payload.message}`));
    }).then(fn => {
      unlistenErr = fn;
    });

    // Invoke the Rust command (triggers the SSE bridge)
    invoke<ChatChunk[]>('agent_chat', { request }).catch(err => {
      cleanup();
      reject(err);
    });
  });
}

/**
 * Resolve the WebSocket base URL from an HTTP API base URL.
 *
 * `http://host:port` -> `ws://host:port`, `https://` -> `wss://`,
 * and strips any trailing slashes.
 */
function resolveWsBaseUrl(apiBaseUrl: string): string {
  return apiBaseUrl.replace(/^https?:\/\//, m => (m === 'https://' ? 'wss://' : 'ws://')).replace(/\/+$/, '');
}

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

/**
 * High-level streamed agent chat, following the unified bridge convention.
 *
 * - **Tauri**: invokes `agent_chat` IPC and consumes `agent:stream:*` Tauri Events.
 * - **Browser**: opens a WebSocket to `/sessions/agent/ws`.
 *
 * Returns a `StreamController` whose `abort()` stops the generation, plus a
 * Promise that resolves when the stream completes (`done`) and rejects on
 * error or unexpected teardown.
 *
 * @param request  The chat payload.
 * @param onChunk  Called with each text fragment, its semantic type, and the session id.
 * @param onQueued Called when the backend enqueues the message (session busy) instead of streaming immediately.
 * @returns        `{ controller, promise }`.
 */
export function streamChatMessage(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
  onDone?: OnDoneCallback,
  onQueued?: OnQueuedCallback
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  if (isTauri()) {
    const promise = sendChatMessageTauri(request, onChunk, onDone);
    return {
      controller: { closed: false, abort: () => void stopChatMessage(request.session_id || 'default') },
      promise
    };
  }
  return sendChatMessageWs(request, onChunk, onHitl, onDone, onQueued);
}

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
 * @returns        `{ controller, promise }` — `promise` resolves on completion.
 */
/** Media kind — drives the upload endpoint path and the default MIME/content-type. */
export type UploadMediaKind = 'image' | 'audio' | 'video';

/** Per-kind upload endpoint suffix (POST `${baseURL}/${endpoint}`). */
const KIND_ENDPOINT: Record<UploadMediaKind, string> = {
  image: '/images/upload',
  audio: '/audio/upload',
  video: '/video/upload'
};

/** Wildcard MIME (e.g. `image/`, `audio/`, `video/`) to parse a `data:...;base64,` prefix. */
const KIND_MIME_PREFIX: Record<UploadMediaKind, string> = {
  image: 'image/',
  audio: 'audio/',
  video: 'video/'
};

/** Fallback content-type when the payload carries no `data:` prefix. */
const KIND_DEFAULT_CONTENT_TYPE: Record<UploadMediaKind, string> = {
  image: 'image/png',
  audio: 'audio/webm',
  video: 'video/mp4'
};

/** Human-readable kind label used in error messages. */
const KIND_LABEL: Record<UploadMediaKind, string> = {
  image: '图片',
  audio: '音频',
  video: '视频'
};

/**
 * Upload base64 media (image/audio/video) to the backend's corresponding
 * `/images|/audio|/video/upload` endpoint and return the URL list.
 *
 * @param kind       Media kind (image | audio | video); determines the upload endpoint and MIME parsing rules
 * @param base64List List of base64-encoded strings (may carry a data:<mime>;base64, prefix)
 * @param baseURL    Backend HTTP base URL
 * @returns          Array of uploaded URLs (same order as the input)
 */
async function uploadBase64ToUrls(kind: UploadMediaKind, base64List: string[], baseURL: string): Promise<string[]> {
  const label = KIND_LABEL[kind];
  const urls: string[] = [];
  for (const base64 of base64List) {
    let contentType = KIND_DEFAULT_CONTENT_TYPE[kind];
    let pureBase64 = base64;

    // If a data:<mime>;base64, prefix is present, strip it and extract the MIME type
    const match = pureBase64.match(new RegExp(`^data:(${KIND_MIME_PREFIX[kind]}[w.+-]+);base64,(.+)$`));
    if (match) {
      contentType = match[1] ?? contentType;
      pureBase64 = match[2] ?? pureBase64;
    }

    const bytes = Uint8Array.from(atob(pureBase64), c => c.charCodeAt(0));

    let resp: Response;
    try {
      resp = await fetch(`${baseURL}${KIND_ENDPOINT[kind]}`, {
        method: 'POST',
        headers: { 'Content-Type': contentType },
        body: bytes
      });
    } catch (e) {
      throw new Error(`${label}上传网络错误: ${e}`, { cause: e });
    }

    if (!resp.ok) {
      throw new Error(`${label}上传失败: HTTP ${resp.status}`);
    }

    let json: { success?: boolean; url?: string; filename?: string };
    try {
      json = await resp.json();
    } catch {
      throw new Error(`${label}上传失败: 服务器返回非 JSON 响应`);
    }

    if (!json.success || !json.url) {
      throw new Error(`${label}上传失败: ${JSON.stringify(json)}`);
    }

    urls.push(json.url);
  }
  return urls;
}

function sendChatMessageWs(
  request: ChatRequest,
  onChunk: OnChunkCallback,
  onHitl?: OnHitlCallback,
  onDone?: OnDoneCallback,
  onQueued?: OnQueuedCallback
): {
  controller: StreamController;
  promise: Promise<void>;
} {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/sessions/agent/ws`;
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
    if (s) {
      s.onopen = null;
      s.onmessage = null;
      s.onerror = null;
      s.onclose = null;
      if (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING) {
        s.close();
      }
    }
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

    // Upload base64 images to the backend /images/upload to get HTTP URLs
    let imageUrls: string[] = [];
    const imageList = request.image_base64_list;
    if (imageList && imageList.length > 0) {
      try {
        imageUrls = await uploadBase64ToUrls('image', imageList, baseURL);
      } catch (e) {
        if (!done) {
          done = true;
          release(`图片上传失败: ${e}`);
        }
        return;
      }
    }

    // Upload base64 audio to the backend /audio/upload to get HTTP URLs
    let audioUrls: string[] = [];
    const audioList = request.audio_bytes_list;
    if (audioList && audioList.length > 0) {
      try {
        audioUrls = await uploadBase64ToUrls('audio', audioList, baseURL);
      } catch (e) {
        if (!done) {
          done = true;
          release(`音频上传失败: ${e}`);
        }
        return;
      }
    }

    // Upload base64 video to the backend /video/upload to get HTTP URLs
    let videoUrls: string[] = [];
    const videoList = request.video_bytes_list;
    if (videoList && videoList.length > 0) {
      try {
        videoUrls = await uploadBase64ToUrls('video', videoList, baseURL);
      } catch (e) {
        if (!done) {
          done = true;
          release(`视频上传失败: ${e}`);
        }
        return;
      }
    }

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

      ws.onmessage = event => {
        let data: AgentWsEvent;
        try {
          data = JSON.parse(event.data as string) as AgentWsEvent;
        } catch {
          return;
        }
        if (data.event === 'chunk') {
          receivedChunk = true;
          onChunk(data.content ?? '', data.type ?? 'text', data.session_id ?? sessionId, {
            tool_id: data.tool_id,
            tool_name: data.tool_name,
            args: data.args,
            error: data.error
          });
        } else if (data.event === 'hitl_request') {
          // HITL interrupt: the agent needs human approval; invoke the onHitl callback (silently ignored when absent)
          if (onHitl && data.content) {
            onHitl(data.content as unknown as HitlInterruptData);
          }
        } else if (data.event === 'queued') {
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
        } else if (data.event === 'done') {
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
        } else if (data.event === 'error') {
          if (!done) {
            done = true;
            clearReconnectTimer();
            closeSocket();
            release(data.content || 'WebSocket stream error');
          }
        }
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
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/sessions/agent/ws`;

  let socket: WebSocket | null = null;
  let done: boolean = false;
  let release: (err?: string) => void = () => {};

  const closeSocket = () => {
    const s = socket;
    socket = null;
    if (s) {
      s.onopen = null;
      s.onmessage = null;
      s.onerror = null;
      s.onclose = null;
      if (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING) {
        s.close();
      }
    }
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

    socket.onmessage = event => {
      let data: AgentWsEvent;
      try {
        data = JSON.parse(event.data as string) as AgentWsEvent;
      } catch {
        return;
      }
      if (data.event === 'chunk') {
        onChunk(data.content ?? '', data.type ?? 'text', data.session_id ?? sessionId, {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error
        });
      } else if (data.event === 'hitl_request') {
        // Sequential HITL: the resumed execution paused again; forward to the caller to re-show the approval card
        if (onHitl && data.content) {
          onHitl(data.content as unknown as HitlInterruptData);
        }
      } else if (data.event === 'done' || data.event === 'stopped') {
        if (!done) {
          done = true;
          closeSocket();
          release();
        }
      } else if (data.event === 'error') {
        if (!done) {
          done = true;
          closeSocket();
          release(data.content || 'WebSocket resume error');
        }
      }
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

// ── Stop generation ──────────────────────────────────────

/**
 * Stop an ongoing agent generation.
 */
export async function stopChatMessage(sessionId: string): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('agent_stop', { request: { session_id: sessionId } });
  } else {
    await stopChatMessageBrowser(sessionId);
  }
}

/**
 * Browser mode: send a stop command over the agent WebSocket
 * (`/sessions/agent/ws`) instead of relying on the legacy HTTP stop endpoint.
 */
function stopChatMessageBrowser(sessionId: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const baseURL: string = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
    const wsBase = baseURL.replace(/^https?:\/\//, m => (m === 'https://' ? 'wss://' : 'ws://'));
    const url = `${wsBase.replace(/\/+$/, '')}/sessions/agent/ws`;

    let socket: WebSocket | null = null;

    try {
      socket = new WebSocket(url);
    } catch (e) {
      reject(e);
      return;
    }

    // Resolve once the server acknowledges the stop.
    socket.onmessage = event => {
      try {
        const data = JSON.parse(event.data as string);
        if (data && data.event === 'stopped' && data.session_id === sessionId) {
          cleanup();
          resolve();
        }
      } catch {
        // ignore non-JSON frames
      }
    };

    socket.onopen = () => {
      socket?.send(JSON.stringify({ type: 'stop', session_id: sessionId }));
    };

    socket.onerror = () => {
      cleanup();
      reject(new Error(`WebSocket stop failed: ${url}`));
    };

    socket.onclose = () => {
      cleanup();
      reject(new Error('WebSocket closed before stop confirmation'));
    };

    const cleanup = () => {
      if (socket) {
        socket.onmessage = null;
        socket.onopen = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
        socket = null;
      }
    };
  });
}

// ── Session ──────────────────────────────────────────────

/**
 * Clear all state for a session.
 */
export async function clearSession(sessionId: string): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('session_clear', { request: { session_id: sessionId } });
  } else {
    await fetchApi({
      url: '/sessions',
      opts: { session_id: sessionId },
      method: 'delete'
    });
  }
}

/**
 * A single sub-agent run record surfaced to the "background tasks" tab.
 *
 * Mirrors the serialized `SubagentRunRecord` returned by `GET /subagents/runs`
 * on the backend. Only the public/safe subset of fields is exposed.
 */
export interface SubagentRun {
  run_id: string;
  task_run_id?: string | null;
  child_session_key: string;
  requester_session_key: string;
  task: string;
  task_name?: string | null;
  label?: string | null;
  spawn_mode?: string;
  context_mode?: string;
  agent_id?: string;
  depth?: number;
  role?: string;
  control_scope?: string;
  generation?: number;
  swarm_group_id?: string | null;
  swarm_run_state?: string | null;
  ended_reason?: string | null;
  pause_reason?: string | null;
  execution: {
    status: string;
    started_at: number | null;
    ended_at: number | null;
    outcome: { status: string; error: string | null } | null;
    transcript_target?: string | null;
  };
  completion: {
    required: boolean;
    result_text: string | null;
    captured_at: number | null;
  };
  delivery: {
    status: string;
    payload?: string | null;
    attempt_count?: number;
    last_error?: string | null;
    last_attempt_at?: number | null;
    suspended_at?: number | null;
    discard_reason?: string | null;
    delivered_at?: number | null;
  };
}

/**
 * Fetch the list of sub-agent runs spawned under a session (background tasks).
 *
 * In Tauri mode this calls the matching IPC command; in browser mode it hits
 * the Python `GET /subagents/runs` endpoint directly.
 *
 * @param sessionId Session whose descendant sub-agent runs should be returned.
 * @param scope "descendants" (default) returns the full spawned tree;
 *              "controller" returns runs where the session is requester or child.
 */
export async function fetchSubagentRuns(
  sessionId: string,
  scope: 'descendants' | 'controller' = 'descendants'
): Promise<SubagentRun[]> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<{ runs: SubagentRun[] }>('subagent_runs', {
      request: { session_id: sessionId, scope }
    });
    return resp.runs ?? [];
  }
  const res: ApiResponse = await fetchApi({
    url: '/subagents/runs',
    opts: { session_id: sessionId, scope },
    method: 'get'
  });
  const resp = (res as unknown as { runs?: SubagentRun[] }).runs ?? [];
  return Array.isArray(resp) ? resp : [];
}

/**
 * Fetch a single sub-agent run plus its entire descendant subtree (background tasks).
 *
 * Used to perform a scoped refresh of only the currently focused task tree box,
 * instead of re-fetching the whole session.
 *
 * In Tauri mode this calls the matching IPC command; in browser mode it hits
 * the Python `GET /subagents/runs?run_id=...` endpoint directly.
 *
 * @param runId The root run whose subtree (including itself) should be returned.
 */
export async function fetchSubagentRunSubtree(runId: string): Promise<SubagentRun[]> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<{ runs: SubagentRun[] }>('subagent_runs', {
      request: { run_id: runId }
    });
    return resp.runs ?? [];
  }
  const res: ApiResponse = await fetchApi({
    url: '/subagents/runs',
    opts: { run_id: runId },
    method: 'get'
  });
  const resp = (res as unknown as { runs?: SubagentRun[] }).runs ?? [];
  return Array.isArray(resp) ? resp : [];
}

/**
 * Delete a sub-agent run and its entire descendant subtree (background tasks).
 *
 * Permanently removes the root run plus all of its descendants from the
 * backend registry (in-memory + SQLite) and clears its attachments dir.
 *
 * In Tauri mode this calls the matching IPC command; in browser mode it hits
 * the Python `DELETE /subagents/runs` endpoint directly.
 *
 * @param runId The root run id whose subtree should be removed.
 * @returns The number of runs removed from the backend.
 */
export async function deleteSubagentRunSubtree(runId: string): Promise<number> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<{ success: boolean; removed: number }>('subagent_run_delete', {
      request: { run_id: runId }
    });
    return resp?.removed ?? 0;
  }
  const res: ApiResponse = await fetchApi({
    url: '/subagents/runs',
    opts: { run_id: runId },
    method: 'delete'
  });
  const resp = (res as unknown as { success?: boolean; removed?: number }) ?? {};
  return typeof resp.removed === 'number' ? resp.removed : 0;
}

/**
 * Steer (redirect / resume) a running sub-agent run (background tasks).
 *
 * Cancels the run's current execution and re-dispatches the child agent on the
 * SAME checkpointer thread with the new direction injected (generation +1), so
 * the child continues from its persisted conversation state. Works for
 * RUNNING/INTERRUPTED runs; an empty payload simply resumes an interrupted
 * run. Also revives a run orphaned by a backend restart (zombie RUNNING).
 *
 * Browser/Tauri webview mode hits the Python `POST /subagents/steer` endpoint
 * directly (backend sends `Access-Control-Allow-Origin: *`, so no dedicated
 * Tauri IPC command is required).
 *
 * @param runId The run to steer.
 * @param payload New task and/or additional instructions; both optional.
 * @returns The updated run record, or null when the backend rejected the steer
 *          (terminal/collector state, rate-limited, control denied) or the
 *          request failed.
 */
export async function steerSubagentRun(
  runId: string,
  payload: { new_task?: string; new_instructions?: string } = {}
): Promise<SubagentRun | null> {
  const res: ApiResponse = await fetchApi({
    url: '/subagents/steer',
    opts: { run_id: runId, ...payload },
    method: 'post'
  });
  const resp = (res as unknown as { run?: SubagentRun }) ?? {};
  return resp.run ?? null;
}

/**
 * Retrieve conversation history.
 */
export async function getHistory(sessionId: string, lastTurnCount: number = 10): Promise<HistoryMessage[]> {
  if (isTauri()) {
    const invoke = await getInvoke();
    return invoke<HistoryMessage[]>('session_history', {
      request: { session_id: sessionId, last_turn_count: lastTurnCount }
    });
  }
  return fetchApi({
    url: '/n_turns_history_messages',
    opts: { session_id: sessionId, last_turn_count: lastTurnCount },
    method: 'get'
  }) as unknown as Promise<HistoryMessage[]>;
}

// ── System Prompt ────────────────────────────────────────

/**
 * Read all system prompt files.
 */
export async function readSystemPrompt(): Promise<Record<string, string>> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<PromptFileResponse>('system_prompt_read');
    return resp.file_to_content;
  }
  return fetchApi({ url: '/system_prompt', method: 'get' }) as unknown as Promise<Record<string, string>>;
}

/**
 * Read the persona template files for a given language (e.g. restore-default).
 *
 * The templates live under `workspace/template/<lang>/`. When `lang` is omitted
 * the backend falls back to the user's preferred workspace template language.
 */
export async function readSystemPromptTemplate(lang?: string): Promise<Record<string, string>> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<PromptFileResponse>('system_prompt_read_template', {
      payload: { lang: lang ?? null }
    });
    return resp.file_to_content;
  }
  return fetchApi({
    url: '/system_prompt/template',
    opts: { lang: lang ?? undefined },
    method: 'get'
  }) as unknown as Promise<Record<string, string>>;
}

/**
 * Overwrite system prompt files (full replacement).
 */
export async function writeSystemPrompt(fileToContent: Record<string, string>): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('system_prompt_write', { payload: { file_to_content: fileToContent } });
  } else {
    await fetchApi({
      url: '/system_prompt',
      opts: { file_to_content: fileToContent },
      method: 'put'
    });
  }
}

/**
 * Partially update system prompt files (merge).
 */
export async function updateSystemPrompt(fileToContent: Record<string, string>): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('system_prompt_update', { payload: { file_to_content: fileToContent } });
  } else {
    await fetchApi({
      url: '/system_prompt',
      opts: { file_to_content: fileToContent },
      method: 'put'
    });
  }
}

// ── Long-term Memory ────────────────────────────────────

/**
 * Read all long-term memory files (workspace/memory/*).
 *
 * Note: `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests. The timestamp query param is a legacy cache-buster from the
 * previous `useFetch`-based implementation and is now harmless (kept so the
 * backend URL shape is unchanged).
 */
export async function readMemory(): Promise<Record<string, string>> {
  if (isTauri()) {
    const invoke = await getInvoke();
    const resp = await invoke<PromptFileResponse>('memory_read');
    return resp.file_to_content;
  }
  return fetchApi({
    url: '/memory',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<Record<string, string>>;
}

/**
 * Overwrite long-term memory files (full replacement).
 * Only provided files are overwritten; others are left unchanged.
 */
export async function writeMemory(fileToContent: Record<string, string>): Promise<void> {
  if (isTauri()) {
    const invoke = await getInvoke();
    await invoke('memory_write', { payload: { file_to_content: fileToContent } });
  } else {
    await fetchApi({
      url: '/memory',
      opts: { file_to_content: fileToContent },
      method: 'put'
    });
  }
}

// ── Heartbeat ────────────────────────────────────

/**
 * Read the heartbeat file (`workspace/HEARTBEAT.md`).
 *
 * Unlike memory, heartbeat deliberately skips Rust/Tauri and always goes
 * through `fetchApi` in both modes (there is no Rust command for heartbeat).
 * `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests; the timestamp query param is a legacy cache-buster from
 * the previous `useFetch`-based implementation and is now harmless.
 */
export async function readHeartbeat(): Promise<Record<string, string>> {
  return fetchApi({
    url: '/heartbeat',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<Record<string, string>>;
}

/**
 * Overwrite the heartbeat file (`workspace/HEARTBEAT.md`, full replacement).
 * Always uses `fetchApi` in both modes — no Rust/Tauri command exists for
 * heartbeat.
 */
export async function writeHeartbeat(fileToContent: Record<string, string>): Promise<void> {
  await fetchApi({
    url: '/heartbeat',
    opts: { file_to_content: fileToContent },
    method: 'put'
  });
}

// ── Cron (scheduled tasks) ──────────────────────

/** Schedule of a cron job (camelCase JSON shape matching `CronSchedule`). */
export interface CronSchedule {
  kind: 'at' | 'every' | 'cron';
  /** Absolute epoch millis — required when kind === 'at'. */
  atMs?: number | null;
  /** Interval in millis — required when kind === 'every'. */
  everyMs?: number | null;
  /** Cron expression — required when kind === 'cron'. */
  expr?: string | null;
  /** IANA timezone name, optional. */
  tz?: string | null;
}

/** Payload of a cron job (camelCase JSON shape matching `CronPayload`). */
export interface CronPayload {
  kind: string;
  /** The message the job sends when it fires. */
  message: string;
  /** Whether the message should be delivered to a channel. */
  deliver: boolean;
  /** Channel id used when deliver is true. */
  channel?: string | null;
  /** Optional recipient override (e.g. chat_id / user id). */
  to?: string | null;
}

/** Runtime state of a cron job (camelCase JSON shape matching `CronJobState`). */
export interface CronJobState {
  nextRunAtMs?: number | null;
  lastRunAtMs?: number | null;
  lastStatus?: string | null;
  lastError?: string | null;
}

/** A single cron job as returned by the backend `/cron` endpoints. */
export interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  schedule: CronSchedule;
  payload: CronPayload;
  state: CronJobState;
  createdAtMs?: number | null;
  updatedAtMs?: number | null;
  deleteAfterRun: boolean;
}

/** Response of `GET /cron`. */
export interface CronListResponse {
  jobs: CronJob[];
  success?: boolean;
}

/** Response of `POST /cron`, `PUT /cron`, and `POST /cron/enable`. */
export interface CronMutateResponse {
  success: boolean;
  job?: CronJob;
  message?: string;
}

/** Response of `POST /cron/trigger` and `DELETE /cron`. */
export interface CronActionResponse {
  success: boolean;
  message?: string;
}

/**
 * List cron jobs.
 *
 * `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests; the timestamp query param is a legacy cache-buster from
 * the previous `useFetch`-based implementation and is now harmless (repeated
 * calls after add/edit/run always return fresh job state regardless).
 *
 * @param includeDisabled Whether to include jobs whose `enabled` flag is false.
 */
export async function listCronJobs(includeDisabled = false): Promise<CronListResponse> {
  const resp = (await fetchApi({
    url: '/cron',
    opts: { _ts: Date.now(), include_disabled: includeDisabled },
    method: 'get'
  })) as unknown as CronListResponse;
  return resp;
}

/**
 * Create a new cron job.
 *
 * @param input The job fields (mirrors the `POST /cron` body).
 * @returns `{ success, job?, message? }` from the backend.
 */
export async function addCronJob(input: {
  name: string;
  message: string;
  schedule: CronSchedule;
  deliver?: boolean;
  channel?: string | null;
  to?: string | null;
  delete_after_run?: boolean;
}): Promise<CronMutateResponse> {
  return fetchApi({
    url: '/cron',
    opts: input,
    method: 'post'
  }) as unknown as Promise<CronMutateResponse>;
}

/**
 * Update an existing cron job.
 *
 * @param id   The job id to update.
 * @param patch Partial fields to merge onto the existing job.
 * @returns `{ success, job?, message? }` from the backend.
 */
export async function updateCronJob(
  id: string,
  patch: {
    name?: string;
    message?: string;
    schedule?: CronSchedule;
    deliver?: boolean;
    channel?: string | null;
    to?: string | null;
    delete_after_run?: boolean;
  }
): Promise<CronMutateResponse> {
  return fetchApi({
    url: '/cron',
    opts: { id, ...patch },
    method: 'put'
  }) as unknown as Promise<CronMutateResponse>;
}

/**
 * Manually trigger a cron job now.
 *
 * @param id    The job id to run.
 * @param force When false (default) a disabled job is skipped.
 * @returns `{ success, message? }` from the backend.
 */
export async function runCronJob(id: string, force = false): Promise<CronActionResponse> {
  return fetchApi({
    url: '/cron/trigger',
    opts: { id, force },
    method: 'post'
  }) as unknown as Promise<CronActionResponse>;
}

/**
 * Enable or disable a cron job.
 *
 * @param id      The job id.
 * @param enabled Desired activation state (default true).
 * @returns `{ success, job?, message? }` from the backend.
 */
export async function enableCronJob(id: string, enabled = true): Promise<CronMutateResponse> {
  return fetchApi({
    url: '/cron/enable',
    opts: { id, enabled },
    method: 'post'
  }) as unknown as Promise<CronMutateResponse>;
}

/**
 * Remove a cron job. This is irreversible — callers MUST confirm first.
 *
 * @param id The job id to delete.
 * @returns `{ success, message? }` from the backend.
 */
export async function deleteCronJob(id: string): Promise<CronActionResponse> {
  return fetchApi({
    url: '/cron',
    opts: { id },
    method: 'delete'
  }) as unknown as Promise<CronActionResponse>;
}

/**
 * List all skills (builtin, auto, third_party).
 *
 * `fetchApi` resolves through ofetch's `$fetch`, which does not cache or
 * dedupe requests; the timestamp query param is a legacy cache-buster from
 * the previous `useFetch`-based implementation and is now harmless (repeated
 * calls after a curator run or any lifecycle change always return fresh data).
 */
export async function listSkills(): Promise<{ skills: SkillInfo[] }> {
  return fetchApi({
    url: '/skills',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<{ skills: SkillInfo[] }>;
}

/**
 * List all available channels.
 *
 * Mirrors `listSkills`: the timestamp query param is a legacy cache-buster
 * from the previous `useFetch`-based implementation and is now harmless
 * (repeated calls always return fresh channel status either way).
 */
export async function listChannels(): Promise<{ channels: ChannelInfo[] }> {
  return fetchApi({
    url: '/channels',
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<{ channels: ChannelInfo[] }>;
}

/**
 * Read a single skill's full SKILL.md content.
 */
export async function readSkill(location: string): Promise<SkillDetail> {
  const cleanPath = location.replace(/^\.\//, '');
  return fetchApi({ url: `/skills/${cleanPath}`, method: 'get' }) as unknown as Promise<SkillDetail>;
}

/**
 * Upload a third-party skill from a local SKILL.md file.
 *
 * Sends the file as `multipart/form-data` (field `file`) plus an optional
 * `name` field derived from the file's base name without extension. The
 * uploaded skill is inactive by default.
 *
 * @param file The local SKILL.md file to upload.
 * @returns `{ success, message?, name?, warnings? }` from the backend. When
 *   `warnings` is non-empty the upload was accepted (CAUTION scan verdict) but
 *   the security scanner flagged concerns that should be surfaced to the user.
 */
export async function uploadSkill(
  file: File
): Promise<{ success: boolean; message?: string; name?: string; warnings?: string[] }> {
  const formData = new FormData();
  formData.append('file', file);
  const baseName = file.name.replace(/\.[^.]+$/, '');
  formData.append('name', baseName);
  return fetchApi({
    url: '/skills/upload',
    opts: formData,
    method: 'post',
    contentType: 'multipart/form-data'
  }) as unknown as Promise<{ success: boolean; message?: string; name?: string; warnings?: string[] }>;
}

/**
 * Toggle whether a skill is active.
 *
 * @param name The skill name to toggle.
 * @param active The desired activation state.
 * @returns `{ success, message? }` from the backend.
 */
export async function setSkillActive(name: string, active: boolean): Promise<{ success: boolean; message?: string }> {
  return fetchApi({
    url: '/skills/toggle',
    opts: { name, active },
    method: 'post'
  }) as unknown as Promise<{ success: boolean; message?: string }>;
}

/** Response of `POST /skills/delete`. */
export interface DeleteSkillResponse {
  success: boolean;
  name?: string;
  message?: string;
}

/**
 * Delete an auto skill from disk.
 *
 * This is an irreversible operation — the client MUST show a confirmation
 * dialog before calling this. Pinned skills are rejected by the
 * backend (`delete_skill`).
 *
 * @param name The auto skill name to delete.
 * @returns `{ success, name?, message? }` from the backend.
 */
export async function deleteSkill(name: string): Promise<DeleteSkillResponse> {
  return fetchApi({
    url: '/skills/delete',
    opts: { name },
    method: 'post'
  }) as unknown as Promise<DeleteSkillResponse>;
}

/** Response of `POST /skills/pin`. */
export interface PinSkillResponse {
  success: boolean;
  name?: string;
  pinned?: boolean;
  message?: string;
}

/**
 * Pin or unpin an auto skill.
 *
 * Pinned skills are excluded from curator merging/removal and rejected by the
 * backend `delete_skill`. When `pinned` is `true` the skill is protected; when
 * `false` it is returned to normal curator lifecycle.
 *
 * @param name The auto skill name to pin/unpin.
 * @param pinned `true` to pin, `false` to unpin.
 * @returns `{ success, name?, pinned?, message? }` from the backend.
 */
export async function pinSkill(name: string, pinned: boolean): Promise<PinSkillResponse> {
  return fetchApi({
    url: '/skills/pin',
    opts: { name, pinned },
    method: 'post'
  }) as unknown as Promise<PinSkillResponse>;
}

/** Auto-transition counters returned by `run_curator_review` (e.g. marked_stale/archived/reactivated/checked/seeded). */
export interface CuratorAutoTransitions {
  marked_stale: number;
  archived: number;
  reactivated: number;
  checked: number;
  seeded: number;
}

/** Mirrors the result object returned by `context_engine/curator/orchestrator.run_curator_review`. */
export interface CuratorRunResult {
  started_at: string;
  auto_transitions: CuratorAutoTransitions;
  /** Human-readable run summary (e.g. "no changes"). */
  summary_so_far: string;
  /** Present when the LLM consolidation pass failed (e.g. model not configured). */
  error?: string;
}

/** Response of `POST /curator/run`. */
export interface CuratorRunResponse {
  success: boolean;
  result?: CuratorRunResult;
  error?: string;
}

/**
 * Force-trigger a curator review/maintenance run against the auto-learned
 * skills. Calls `run_curator_review` on the backend (the forced entry point,
 * not the idle-scheduled `maybe_run_curator`), so it always executes.
 *
 * @returns `{ success, result, error }` from the backend.
 */
export async function runCuratorReview(): Promise<CuratorRunResponse> {
  return fetchApi({ url: '/curator/run', method: 'post' }) as unknown as Promise<CuratorRunResponse>;
}

/** Settings returned by `GET /curator/settings`. */
export interface CuratorSettings {
  success: boolean;
  /** Auto-maintenance interval override in days, null when unset (falls back to `interval_hours`). */
  auto_interval_days: number | null;
  /** Configured maintenance interval in hours (from curator.yaml). */
  interval_hours: number;
  /** ISO timestamp of the last curator run, null when never run. */
  last_run_at: string | null;
  /** ISO timestamp of the last maintenance, null when never run. */
  last_maintenance_at: string | null;
  error?: string;
}

/** Response of `PUT /curator/settings`. */
export interface CuratorSettingsUpdateResponse {
  success: boolean;
  /** The stored override (null = back to curator.yaml default). */
  auto_interval_days: number | null;
  /** Effective interval in hours (override days x 24, or curator.yaml default). */
  interval_hours: number;
  /** ISO timestamp of the last maintenance, null when never run. */
  last_maintenance_at: string | null;
  error?: string;
}

/**
 * Read the curator auto-maintenance settings.
 *
 * @returns `{ success, auto_interval_days, interval_hours, last_run_at, last_maintenance_at }`.
 */
export async function getCuratorSettings(): Promise<CuratorSettings> {
  return fetchApi({ url: '/curator/settings', method: 'get' }) as unknown as Promise<CuratorSettings>;
}

/**
 * Override the curator auto-maintenance interval.
 *
 * @param days Days between auto-maintenance runs (1-5), or null to use the curator.yaml default.
 * @returns `{ success, auto_interval_days, interval_hours, last_maintenance_at }`.
 */
export async function setCuratorSettings(days: number | null): Promise<CuratorSettingsUpdateResponse> {
  return fetchApi({
    url: '/curator/settings',
    opts: { auto_interval_days: days },
    method: 'put'
  }) as unknown as Promise<CuratorSettingsUpdateResponse>;
}

// ── Health ───────────────────────────────────────────────

/**
 * Check whether the Python backend is reachable.
 */
export async function checkHealth(): Promise<HealthStatus> {
  if (isTauri()) {
    const invoke = await getInvoke();
    return invoke<HealthStatus>('system_health');
  }
  // Browser fallback: try to fetch a lightweight endpoint
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  try {
    const resp = await fetch(`${baseURL}/system_prompt`);
    if (resp.ok) {
      return { healthy: true, message: 'Python backend reachable' };
    }
    return { healthy: false, message: `HTTP ${resp.status}` };
  } catch (e) {
    return { healthy: false, message: String(e) };
  }
}

// ── Skills ───────────────────────────────────────────────

export interface SkillInfo {
  name: string;
  description: string;
  location: string;
  category: 'builtin' | 'auto' | 'third_party';
  /** Whether the skill is excluded from curator maintenance (never merged/removed). */
  pinned?: boolean;
}

/** A single node in a skill's on-disk directory structure (relative to the skill root). */
export interface SkillFileNode {
  /** Relative path from the skill root, e.g. `scripts/core.py` or `references/part01.md`. */
  path: string;
  /** Basename of the file/directory, e.g. `core.py`. */
  name: string;
  /** `file` for regular files, `dir` for directories. */
  type: 'file' | 'dir';
  /** UTF-8 text content — present only on `file` nodes. */
  content?: string;
}

export interface SkillDetail extends SkillInfo {
  content: string;
  /** Recursively-ordered directory structure under the skill's folder (SKILL.md + references/scripts/etc.). */
  files?: SkillFileNode[];
}

// ── Channels ─────────────────────────────────────────────

export interface ChannelInfo {
  name: string;
  display_name: string;
  enabled: boolean;
  /** Persisted runtime toggle; gates scheduled heartbeat delivery (also needs a receiver). */
  heartbeat: boolean;
  /** Persisted runtime toggle for scheduled/cron behavior. */
  cron: boolean;
  icon: string;
}

/** Body for `updateChannel` — only the boolean toggles exposed by the settings UI. */
export interface ChannelUpdate {
  enabled?: boolean;
  heartbeat?: boolean;
  cron?: boolean;
}

/**
 * Persist per-channel runtime toggles (enabled/heartbeat/cron) to the backend.
 *
 * No Tauri IPC command exists for channels yet, so unlike write flows that
 * round-trip through Rust, channel writes go straight to the Python REST API
 * in both modes (mirrors `listChannels`).
 */
export async function updateChannel(channelName: string, update: ChannelUpdate): Promise<ChannelInfo> {
  return fetchApi({
    url: `/channels/${channelName}`,
    opts: { ...update },
    method: 'put'
  }) as unknown as Promise<ChannelInfo>;
}

/**
 * Free-form per-channel config dict, persisted to
 * plugins/channels/<name>/config.json (e.g. { app_id, receiver } for QQ).
 */
export type ChannelConfig = Record<string, unknown>;

/** Body/wrapper of `getChannelConfig`. */
export interface ChannelConfigResponse {
  channel_name: string;
  config: ChannelConfig;
}

/**
 * Read a channel's own config.json (plugins/channels/<name>/config.json),
 * returned as a free-form key/value map so the settings UI can render/edit
 * arbitrary fields (strings, numbers, booleans, lists).
 */
export async function getChannelConfig(channelName: string): Promise<ChannelConfigResponse> {
  return fetchApi({
    url: `/channels/${channelName}/config`,
    opts: { _ts: Date.now() },
    method: 'get'
  }) as unknown as Promise<ChannelConfigResponse>;
}

/**
 * Persist a channel's own config.json wholesale. The dict is stored verbatim,
 * preserving each value's JSON type. Mirrors the PUT semantics of the backend.
 */
export async function updateChannelConfig(channelName: string, config: ChannelConfig): Promise<ChannelConfigResponse> {
  return fetchApi({
    url: `/channels/${channelName}/config`,
    opts: { ...config },
    method: 'put'
  }) as unknown as Promise<ChannelConfigResponse>;
}

// ── Logs ────────────────────────────────────────────────

/** Log level severity, matching the backend log format. */
export type LogLevel = 'TRACE' | 'DEBUG' | 'INFO' | 'SUCCESS' | 'WARNING' | 'ERROR' | 'CRITICAL';

/** Metadata for a single `.log` file on the backend. */
export interface LogFileInfo {
  name: string;
  path: string;
  size: number;
  modified: string;
  is_error: boolean;
  /** True when this is the log written by the currently running backend process. */
  is_current: boolean;
}

/** Response of `GET /logs/files`. */
export interface LogFileList {
  files: LogFileInfo[];
}

/** Response of `GET /logs?path=...&lines=...`. */
export interface LogReadResult {
  success: boolean;
  path: string;
  content: string;
  lines: number;
}

/** A single log record pushed over the live WebSocket. */
export interface LogStreamData {
  timestamp: string;
  level: string;
  name: string;
  function: string;
  line: number;
  message: string;
}

/** A frame pushed by the `/logs/ws` WebSocket. */
export interface LogStreamFrame {
  event: string;
  data?: LogStreamData;
}

/**
 * List all available `.log` files, newest first.
 */
export async function listLogFiles(): Promise<LogFileList> {
  return fetchApi({ url: '/logs/files', method: 'get' }) as unknown as Promise<LogFileList>;
}

/**
 * Read the trailing `lines` of a log file (default 500).
 *
 * @param path  The log file path (from `LogFileInfo.path`).
 * @param lines Number of trailing lines to read.
 */
export async function readLogFile(path: string, lines?: number): Promise<LogReadResult> {
  return fetchApi({
    url: '/logs',
    opts: { path, lines: lines ?? 500 },
    method: 'get'
  }) as unknown as Promise<LogReadResult>;
}

/**
 * Open a live log stream over the backend WebSocket (`/logs/ws`).
 *
 * The server pushes JSON text frames: `{"event": "ready"}` on connect and
 * `{"event": "log", "data": {...}}` for each new log record.
 *
 * @param onFrame Called with each parsed frame.
 * @param onError Called with an error message on connection failure.
 * @returns       A handle whose `close()` tears down the socket.
 */
export function openLogStream(
  onFrame: (frame: LogStreamFrame) => void,
  onError?: (e: string) => void
): { close: () => void } {
  const baseURL = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const url = `${resolveWsBaseUrl(baseURL)}/logs/ws`;

  let socket: WebSocket | null = null;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    const s = socket;
    socket = null;
    if (s) {
      s.onopen = null;
      s.onmessage = null;
      s.onerror = null;
      s.onclose = null;
      if (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING) {
        s.close();
      }
    }
  };

  try {
    socket = new WebSocket(url);
  } catch (e) {
    onError?.(String(e));
    return { close };
  }

  socket.onopen = () => {
    // No action needed; the server pushes a "ready" frame on connect.
  };

  socket.onmessage = event => {
    let frame: LogStreamFrame;
    try {
      frame = JSON.parse(event.data as string) as LogStreamFrame;
    } catch {
      return;
    }
    onFrame(frame);
  };

  socket.onerror = () => {
    onError?.('WebSocket connection error');
  };

  socket.onclose = () => {
    if (!closed) {
      closed = true;
      onError?.('WebSocket closed');
    }
  };

  return { close };
}
