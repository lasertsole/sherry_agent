/**
 * Per-session persistent agent WebSocket (`/sessions/agent/ws`).
 *
 * Browser-mode chat uses exactly ONE WebSocket per session (registered by
 * `session_id`), reused for every send, stop and HITL response. Replies can
 * therefore never be misrouted to another send: frames only carry a
 * `session_id`, so routing is session-level (`turn_started` -> `activeTurn` ->
 * `onChunk`), while `done` / `error` / `stopped` resolve or reject the pending
 * sends listed in `message_ids` (falling back to `activeTurn.msgIds`).
 *
 * The connection mirrors the `ws.ts` singleton pattern (cached CONNECTING
 * singleton, superseded-socket guard) and reconnects with exponential backoff
 * (reusing the constants in `chat-types.ts`), then falls back to a fixed 5s
 * liveness reconnect once the retry budget is exhausted.
 *
 * @module bridge/agentSocket
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
import {
  StreamInterruptedError,
  WS_FALLBACK_RECONNECT_MS,
  WS_RECONNECT_MAX_ATTEMPTS,
  wsReconnectDelayMs
} from './chat-types';
import { KIND_LABEL, uploadBase64ToUrls, type UploadMediaKind } from './upload';
import { teardownWebSocket } from './transport';
import { createWsMessageHandler } from '../ws-message';
import { emit } from '../mitt';
import { API_BASE_URL, WS_BASE_URL } from '../env';

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

/** A single in-flight send awaiting its turn's `done` / `error` / `stopped`. */
interface PendingSend {
  msgId: string;
  request: ChatRequest;
  /** Serialized payload, filled once any base64 media finished uploading. */
  payload: string | null;
  /** At least one chunk of this turn was received (disconnect after this must never resend). */
  receivedChunk: boolean;
  settled: boolean;
  resolve: () => void;
  reject: (err: unknown) => void;
}

/** A resolved media-URL set attached to one outgoing payload. */
interface MediaUrls {
  images: string[];
  audios: string[];
  videos: string[];
}

/**
 * The session-scoped socket handle returned by {@link acquireAgentSocket}.
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

/** Registry: exactly one socket per session id. */
const sockets = new Map<string, SessionAgentSocket>();

/**
 * Acquire (or create) the persistent socket for a session.
 * @param sessionId
 */
export function acquireAgentSocket(sessionId: string): AgentSocket {
  const key = sessionId || 'default';
  const existing = sockets.get(key);
  if (existing) return existing;
  const created = new SessionAgentSocket(key);
  sockets.set(key, created);
  return created;
}

/** Release the session's socket, closing it and settling pending sends. */
export function releaseAgentSocket(sessionId: string): void {
  const key = sessionId || 'default';
  const socket = sockets.get(key);
  if (!socket) return;
  sockets.delete(key);
  socket.dispose();
}

/** Close every agent socket (app teardown / tests). */
export function closeAllAgentSockets(): void {
  for (const socket of sockets.values()) socket.dispose();
  sockets.clear();
}

class SessionAgentSocket implements AgentSocket {
  readonly sessionId: string;

  private socket: WebSocket | null = null;
  private handlers: AgentSocketHandlers = {};
  private pendingSends = new Map<string, PendingSend>();
  private activeTurn: { turnId: string; msgIds: string[] } | null = null;
  private outboundQueue: string[] = [];
  private stopResolvers: Array<() => void> = [];
  private attempt = 0;
  private reconnectScheduled = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  constructor(sessionId: string) {
    this.sessionId = sessionId;
    this.connect();
  }

  setHandlers(handlers: AgentSocketHandlers): void {
    // Merge only defined handlers: per-send callbacks (chunk/onDone/queued) are
    // merged in by `sendChatMessageWs`, and an absent one must never wipe the
    // page's once-installed session-level handler (notably `onTurnStarted`).
    for (const key of Object.keys(handlers) as (keyof AgentSocketHandlers)[]) {
      const value = handlers[key];
      if (value !== undefined) {
        (this.handlers as Record<string, unknown>)[key] = value;
      }
    }
  }

  send(request: ChatRequest): { controller: StreamController; promise: Promise<void> } {
    const msgId = request.msg_id ?? generateId();
    let resolveFn: () => void = () => {};
    let rejectFn: (err: unknown) => void = () => {};
    const pending: PendingSend = {
      msgId,
      request,
      payload: null,
      receivedChunk: false,
      settled: false,
      resolve: () => {},
      reject: () => {}
    };
    const promise = new Promise<void>((resolve, reject) => {
      resolveFn = resolve;
      rejectFn = reject;
    });
    pending.resolve = () => {
      if (pending.settled) return;
      pending.settled = true;
      this.pendingSends.delete(msgId);
      resolveFn();
    };
    pending.reject = (err: unknown) => {
      if (pending.settled) return;
      pending.settled = true;
      this.pendingSends.delete(msgId);
      rejectFn(err instanceof Error ? err : new Error(String(err)));
    };
    this.pendingSends.set(msgId, pending);

    const controller: StreamController = {
      get closed() {
        return pending.settled;
      },
      abort: () => {
        if (pending.settled) return;
        // User-initiated abort: send stop and abandon all in-flight sends of the
        // session. The promises stay pending (no rejection) — matching the
        // long-standing abort contract consumed by `postAgentStream`.
        this.sendStopFrame();
        this.abandonAll();
      },
      sendHitlResponse: (response: HitlResponse) => {
        // A settled (done/aborted) send must not emit frames on the socket.
        if (pending.settled) return;
        this.sendHitlResponse(response);
      }
    };

    void this.dispatch(pending);
    return { controller, promise };
  }

  stop(): Promise<void> {
    const promise = new Promise<void>(resolve => {
      this.stopResolvers.push(resolve);
    });
    this.sendStopFrame();
    // A stop targets the session's generation: abandon every in-flight send (the
    // promises stay pending) so no error is surfaced for a user-initiated stop.
    this.abandonAll();
    return promise;
  }

  sendHitlResponse(response: HitlResponse): void {
    this.sendFrame(
      JSON.stringify({
        type: 'hitl_response',
        session_id: this.sessionId,
        decision: response.decision,
        message: response.message ?? '',
        edited_args: response.edited_args
      })
    );
  }

  dispose(): void {
    this.disposed = true;
    this.clearReconnectTimer();
    // Settle (resolve) rather than reject: teardown must not surface an error.
    for (const pending of [...this.pendingSends.values()]) pending.resolve();
    this.stopResolvers.splice(0).forEach(resolve => resolve());
    const s = this.socket;
    this.socket = null;
    teardownWebSocket(s);
  }

  // ── outbound ─────────────────────────────────────────────

  /** Upload media then enqueue the payload (synchronously when there is none). */
  private async dispatch(pending: PendingSend): Promise<void> {
    const request = pending.request;
    const hasMedia =
      (request.image_base64_list?.length ?? 0) > 0 ||
      (request.audio_bytes_list?.length ?? 0) > 0 ||
      (request.video_bytes_list?.length ?? 0) > 0;
    if (!hasMedia) {
      this.finishPreparation(pending, { images: [], audios: [], videos: [] });
      return;
    }
    try {
      const urls = await this.uploadAll(request);
      if (pending.settled || this.disposed) return;
      this.finishPreparation(pending, urls);
    } catch (e) {
      pending.reject(e);
    }
  }

  /** Serialize the payload and send it now (OPEN) or on the next `onopen`. */
  private finishPreparation(pending: PendingSend, urls: MediaUrls): void {
    pending.payload = JSON.stringify({
      session_id: this.sessionId,
      msg_id: pending.msgId,
      multi_modal_message: {
        text: pending.request.text || '',
        image_base64_list: [],
        image_path_list: urls.images,
        audio_bytes_list: [],
        audio_path_list: urls.audios,
        video_bytes_list: [],
        video_path_list: urls.videos
      }
    });
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(pending.payload);
    }
  }

  private async uploadAll(request: ChatRequest): Promise<MediaUrls> {
    const upload = async (kind: UploadMediaKind, list?: string[]): Promise<string[]> => {
      if (!list || list.length === 0) return [];
      try {
        return await uploadBase64ToUrls(kind, list, API_BASE_URL);
      } catch (e) {
        throw new Error(`${KIND_LABEL[kind]} upload failed: ${e}`);
      }
    };
    return {
      images: await upload('image', request.image_base64_list),
      audios: await upload('audio', request.audio_bytes_list),
      videos: await upload('video', request.video_bytes_list)
    };
  }

  private sendStopFrame(): void {
    this.sendFrame(JSON.stringify({ type: 'stop', session_id: this.sessionId }));
  }

  private sendFrame(frame: string): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(frame);
    } else if (!this.disposed) {
      this.outboundQueue.push(frame);
    }
  }

  private flushOutboundQueue(): void {
    const queue = this.outboundQueue;
    this.outboundQueue = [];
    for (const frame of queue) this.socket?.send(frame);
  }

  // ── connection lifecycle ─────────────────────────────────

  private connect(): void {
    if (this.disposed) return;
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${WS_BASE_URL}/sessions/agent/ws`);
    } catch {
      this.handleConnectionLoss();
      return;
    }
    this.socket = ws;

    ws.onopen = () => {
      if (this.socket !== ws) {
        ws.close();
        return;
      }
      if (this.attempt > 0) emit('stream:reconnected', { sessionId: this.sessionId });
      this.attempt = 0;
      this.reconnectScheduled = false;
      // (Re)send every pending send that has not received a chunk yet; the
      // backend persists a round only when the agent graph completes, so a
      // pre-chunk reconnect is safe (a mid-stream one was already rejected).
      for (const pending of this.pendingSends.values()) {
        if (pending.payload && !pending.receivedChunk) ws.send(pending.payload);
      }
      this.flushOutboundQueue();
    };

    ws.onmessage = (event: MessageEvent) => {
      if (this.socket !== ws) return;
      this.handleFrame(event);
    };

    ws.onerror = () => {
      if (this.socket !== ws) return;
      this.handleConnectionLoss();
    };

    ws.onclose = () => {
      if (this.socket !== ws) return;
      this.handleConnectionLoss();
    };
  }

  private handleConnectionLoss(): void {
    if (this.disposed || this.reconnectScheduled) return;
    this.reconnectScheduled = true;
    this.clearReconnectTimer();

    // Case B: a send already produced chunks — its content is on screen and
    // resending would duplicate it. Fail it immediately and never resend.
    const midStream = [...this.pendingSends.values()].filter(p => p.receivedChunk);
    if (midStream.length > 0) {
      for (const pending of midStream) {
        pending.reject(new StreamInterruptedError('WebSocket closed after streaming began', true));
      }
      emit('ws:conn-loss', { sessionId: this.sessionId, midStream: true });
    }

    // Case A: sends that have not produced a chunk can be re-sent after a reconnect.
    const resumable = [...this.pendingSends.values()].filter(p => !p.receivedChunk && p.payload);
    if (resumable.length > 0 && this.attempt < WS_RECONNECT_MAX_ATTEMPTS) {
      this.attempt += 1;
      emit('ws:conn-loss', { sessionId: this.sessionId, midStream: false });
      emit('stream:reconnecting', {
        sessionId: this.sessionId,
        attempt: this.attempt,
        maxAttempts: WS_RECONNECT_MAX_ATTEMPTS
      });
      this.scheduleReconnect(wsReconnectDelayMs(this.attempt));
      return;
    }

    if (resumable.length > 0) {
      // Retry budget exhausted: surface the interruption and keep the socket
      // alive for future sends with the fixed fallback cadence.
      for (const pending of resumable) {
        pending.reject(new StreamInterruptedError('WebSocket connection error', false));
      }
      emit('ws:conn-loss', { sessionId: this.sessionId, midStream: false });
      emit('stream:reconnect:failed', { sessionId: this.sessionId });
      this.attempt = 0;
      this.scheduleReconnect(WS_FALLBACK_RECONNECT_MS);
      return;
    }

    // No in-flight send: quietly keep the persistent socket alive.
    this.attempt = 0;
    this.scheduleReconnect(WS_FALLBACK_RECONNECT_MS);
  }

  private scheduleReconnect(delayMs: number): void {
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectScheduled = false;
      if (!this.disposed) this.connect();
    }, delayMs);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  // ── inbound ──────────────────────────────────────────────

  private handleFrame(event: MessageEvent): void {
    const handler = createWsMessageHandler<AgentWsEvent>({
      chunk: data => {
        for (const pending of this.pendingSends.values()) pending.receivedChunk = true;
        this.handlers.onChunk?.(data.content ?? '', data.type ?? 'text', data.session_id ?? this.sessionId, {
          tool_id: data.tool_id,
          tool_name: data.tool_name,
          args: data.args,
          error: data.error
        });
      },
      hitl_request: data => {
        if (this.handlers.onHitl && data.content) {
          this.handlers.onHitl(data.content as unknown as HitlInterruptData);
        }
      },
      queued: data => {
        if (this.handlers.onQueued && typeof data.position === 'number') {
          this.handlers.onQueued({
            sessionId: data.session_id ?? this.sessionId,
            position: data.position,
            queueSize: data.queue_size ?? 0,
            messageId: data.message_id ?? undefined
          });
        }
      },
      turn_started: data => {
        this.activeTurn = { turnId: data.turn_id ?? '', msgIds: data.message_ids ?? [] };
        this.handlers.onTurnStarted?.({
          sessionId: data.session_id ?? this.sessionId,
          turnId: this.activeTurn.turnId,
          messageIds: this.activeTurn.msgIds
        });
      },
      done: data => {
        this.settleResolve(this.turnMessageIds(data.message_ids));
        this.activeTurn = null;
        this.handlers.onDone?.({
          modelName: data.model_name ?? undefined,
          inputTokens: data.input_tokens ?? undefined,
          outputTokens: data.output_tokens ?? undefined
        });
      },
      error: data => {
        this.settleReject(this.turnMessageIds(data.message_ids), new Error(data.content || 'WebSocket stream error'));
        this.activeTurn = null;
      },
      stopped: data => {
        this.settleResolve(this.turnMessageIds(data.message_ids));
        this.activeTurn = null;
        this.stopResolvers.splice(0).forEach(resolve => resolve());
      },
      todo_updated: data => emit('ws:todo_updated', data)
    });
    try {
      handler(event);
    } catch {
      // Non-JSON frame: ignore.
    }
  }

  /** Resolve `message_ids`, falling back to the active turn's member ids, then all pending. */
  private turnMessageIds(messageIds?: string[]): string[] {
    if (messageIds && messageIds.length > 0) return messageIds;
    if (this.activeTurn && this.activeTurn.msgIds.length > 0) return this.activeTurn.msgIds;
    return [...this.pendingSends.keys()];
  }

  private settleResolve(messageIds: string[]): void {
    const ids = messageIds.length > 0 ? messageIds : [...this.pendingSends.keys()];
    for (const id of ids) this.pendingSends.get(id)?.resolve();
  }

  private settleReject(messageIds: string[], err: unknown): void {
    const ids = messageIds.length > 0 ? messageIds : [...this.pendingSends.keys()];
    for (const id of ids) this.pendingSends.get(id)?.reject(err);
  }

  /**
   * Silently drop every in-flight send of the session: mark each settled and
   * remove it from the registry WITHOUT settling its promise. Used by a
   * user-initiated stop/abort, where the long-standing contract keeps the
   * returned promise pending (so `postAgentStream` never fires `onError`).
   */
  private abandonAll(): void {
    for (const pending of [...this.pendingSends.values()]) {
      if (pending.settled) continue;
      pending.settled = true;
      this.pendingSends.delete(pending.msgId);
    }
  }
}

/** Generate a protocol `msg_id` (RFC4122 v4 UUID). */
function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
