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
 * Supporting state is split into focused siblings: `agent-socket-pending.ts`
 * (in-flight sends), `agent-socket-queue.ts` (outbound buffering),
 * `agent-socket-reconnect.ts` (backoff policy + timer) and
 * `agent-socket-upload.ts` (media upload + payload serialization); this class
 * owns only orchestration and the connection lifecycle.
 *
 * @module bridge/agentSocket
 */
import type { ChatRequest, HitlResponse, StreamController } from './chat-types';
import { StreamInterruptedError, WS_RECONNECT_MAX_ATTEMPTS } from './chat-types';
import type { AgentSocket, AgentSocketHandlers } from './agent-socket-types';
import { teardownWebSocket } from './transport';
import { emit } from '../mitt';
import { WS_BASE_URL } from '../env';
import { ensureGatewayToken, withGatewayToken } from '../requestApi';
import { OutboundQueue } from './agent-socket-queue';
import { PendingSendRegistry, type PendingSend } from './agent-socket-pending';
import { decideReconnect, ReconnectTimer } from './agent-socket-reconnect';
import { buildAgentPayload, requestHasMedia, uploadRequestMedia, type MediaUrls } from './agent-socket-upload';
import { routeAgentFrame } from './agent-socket-frames';

// Public protocol types stay available at the historical module path
// (`bridge.ts` re-exports them from here).
export type { AgentSocket, AgentSocketHandlers, QueuedAckInfo, TurnStartedInfo } from './agent-socket-types';

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

/**
 * Release the session's socket, closing it and settling pending sends.
 * @param sessionId
 */
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
  private readonly sends = new PendingSendRegistry();
  private readonly outboundQueue = new OutboundQueue();
  private readonly reconnectTimer = new ReconnectTimer();
  private activeTurn: { turnId: string; msgIds: string[] } | null = null;
  /** Highest frame seq covered by the last resume snapshot (see ctx wiring). */
  private snapshotSeq = 0;
  /** Turn the snapshot seq belongs to (the watermark is per-turn). */
  private snapshotTurnId = '';
  /** Frames held while an HTTP history+snapshot request is in flight. */
  private framesHeld = false;
  private heldFrames: MessageEvent[] = [];
  private static readonly MAX_HELD_FRAMES = 1000;
  private stopResolvers: Array<() => void> = [];
  private attempt = 0;
  private reconnectScheduled = false;
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
    const { pending, promise } = this.sends.create(msgId, request);

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
        this.sends.abandonAll();
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
    this.sends.abandonAll();
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

  holdFrames(): void {
    this.framesHeld = true;
  }

  releaseFrames(): void {
    this.framesHeld = false;
    const held = this.heldFrames.splice(0);
    for (const event of held) this.routeFrame(event);
  }

  setSnapshotWatermark(seq: number, turnId = ''): void {
    this.snapshotSeq = seq;
    this.snapshotTurnId = turnId;
  }

  hasPendingSends(): boolean {
    return this.sends.keys().length > 0;
  }

  hasPendingMidStreamSends(): boolean {
    return this.sends.hasReceivedChunk();
  }

  settlePendingSends(): void {
    this.sends.resolveAll();
    this.activeTurn = null;
  }

  settlePendingMidStreamSends(): void {
    this.sends.resolveMidStream();
    this.activeTurn = null;
  }

  dispose(): void {
    this.disposed = true;
    this.framesHeld = false;
    this.heldFrames.length = 0;
    this.reconnectTimer.clear();
    // Settle (resolve) rather than reject: teardown must not surface an error.
    this.sends.resolveAll();
    this.stopResolvers.splice(0).forEach(resolve => resolve());
    const s = this.socket;
    this.socket = null;
    teardownWebSocket(s);
  }

  // ── outbound ─────────────────────────────────────────────

  /**
   * Upload media then enqueue the payload (synchronously when there is none).
   * @param pending
   */
  private async dispatch(pending: PendingSend): Promise<void> {
    if (!requestHasMedia(pending.request)) {
      this.finishPreparation(pending, { images: [], audios: [], videos: [] });
      return;
    }
    try {
      const urls = await uploadRequestMedia(pending.request);
      if (pending.settled || this.disposed) return;
      this.finishPreparation(pending, urls);
    } catch (e) {
      pending.reject(e);
    }
  }

  /**
   * Serialize the payload and send it now (OPEN) or on the next `onopen`.
   * @param pending
   * @param urls
   */
  private finishPreparation(pending: PendingSend, urls: MediaUrls): void {
    pending.payload = buildAgentPayload(this.sessionId, pending.msgId, pending.request, urls);
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(pending.payload);
    }
  }

  private sendStopFrame(): void {
    this.sendFrame(JSON.stringify({ type: 'stop', session_id: this.sessionId }));
  }

  /**
   * Void a queued message (toolbar's queue list). The backend answers
   * `queued_cancelled`; a `false` ack means the row was already delivered.
   * @param msgId
   */
  sendCancelQueued(msgId: string): void {
    this.sendFrame(JSON.stringify({ type: 'cancel_queued', session_id: this.sessionId, msg_id: msgId }));
  }

  /**
   * Continue the session's interrupted answer (the chat row's 继续生成 button).
   * The backend validates the breakpoint and answers `continue_generation_ack`
   * first; only an `ok` ack is followed by chunks.
   */
  sendContinueGeneration(): void {
    this.sendFrame(JSON.stringify({ type: 'continue_generation', session_id: this.sessionId }));
  }

  /**
   * Edit a SENT user message: the backend truncates that turn onward and re-runs
   * the edited text as a new turn. Failures arrive as `edit_message_ack`.
   * @param turnNum The edited message's turn number.
   * @param newText The replacement text.
   * @param msgId A FRESH client msg id (the queue dedupes by it).
   * @param rowId The persisted row id (identifies the exact message).
   */
  sendEditMessage(turnNum: number, newText: string, msgId: string, rowId?: number): void {
    this.sendFrame(
      JSON.stringify({
        type: 'edit_message',
        session_id: this.sessionId,
        turn_num: turnNum,
        new_text: newText,
        msg_id: msgId,
        ...(rowId && rowId > 0 ? { row_id: rowId } : {})
      })
    );
  }

  /**
   * Edit a queued message's text before it is delivered.
   * @param msgId
   * @param message
   */
  sendEditQueued(msgId: string, message: string): void {
    this.sendFrame(
      JSON.stringify({
        type: 'edit_queued',
        session_id: this.sessionId,
        msg_id: msgId,
        message
      })
    );
  }

  /**
   * Interrupt the running turn so this queued message is delivered next. The
   * backend moves it to the front of the FIFO queue first, then cancels the
   * turn; `send_now_ack` reports whether the move happened.
   * @param msgId
   */
  sendNow(msgId: string): void {
    this.sendFrame(JSON.stringify({ type: 'send_now', session_id: this.sessionId, msg_id: msgId }));
  }

  /**
   * Stop ONE background command of this session. Command-level: the turn keeps
   * streaming and the session's other background tasks are untouched. The
   * backend answers `terminal_bg_kill_ack` (signal landed) and later
   * `terminal_bg_exit` (the final state).
   * @param taskId The task id from terminal(run_in_background=True).
   */
  sendTerminalBgKill(taskId: string): void {
    this.sendFrame(JSON.stringify({ type: 'terminal_bg_kill', session_id: this.sessionId, task_id: taskId }));
  }

  private sendFrame(frame: string): void {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(frame);
    } else if (!this.disposed) {
      this.outboundQueue.enqueue(frame);
    }
  }

  private flushOutboundQueue(): void {
    this.outboundQueue.flush(frame => this.socket?.send(frame));
  }

  // ── connection lifecycle ─────────────────────────────────

  private connect(): void {
    if (this.disposed) return;
    let ws: WebSocket;
    try {
      void ensureGatewayToken();
      // Prefetch the login gate's single-use ticket (see composables/ws-ticket.ts);
      // the URL below spends whatever is cached. Loopback clients need none.
      void prepareWsTicket();
      ws = new WebSocket(appendWsTicket(withGatewayToken(`${WS_BASE_URL}/sessions/agent/ws`)));
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
      for (const pending of this.sends.values()) {
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
    this.reconnectTimer.clear();

    const inFlight = this.sends.all();

    // A send that already produced chunks is NOT rejected here any more: the
    // server keeps the turn running, and after the reconnect the page fetches
    // history + the active-turn snapshot instead of resending the payload — so
    // nothing is replayed and the content is never duplicated.
    const midStream = inFlight.filter(p => p.receivedChunk);
    // Sends that never produced a chunk can be re-sent after a reconnect; the
    // backend dedupes on the client msg_id, so a turn it already started is
    // never duplicated.
    const resumable = inFlight.filter(p => !p.receivedChunk && p.payload);
    const decision = decideReconnect(midStream.length + resumable.length > 0, this.attempt);

    if (decision.retrying) {
      this.attempt = decision.attempt;
      emit('stream:reconnecting', {
        sessionId: this.sessionId,
        attempt: this.attempt,
        maxAttempts: WS_RECONNECT_MAX_ATTEMPTS
      });
      this.scheduleReconnect(decision.delayMs);
      return;
    }

    if (decision.budgetExhausted) {
      // Retry budget exhausted: surface the interruption and keep the socket
      // alive for future sends with the fixed fallback cadence. Mid-stream
      // sends now fail as interrupted too (their turn may still be running
      // server-side; the next successful connect's snapshot would catch up,
      // but the UI must not wait forever).
      for (const pending of inFlight) {
        pending.reject(new StreamInterruptedError('WebSocket connection error', pending.receivedChunk));
      }
      emit('ws:conn-loss', { sessionId: this.sessionId, midStream: midStream.length > 0 });
      emit('stream:reconnect:failed', { sessionId: this.sessionId });
      this.attempt = decision.attempt;
      this.scheduleReconnect(decision.delayMs);
      return;
    }

    // No in-flight send: quietly keep the persistent socket alive.
    this.attempt = decision.attempt;
    this.scheduleReconnect(decision.delayMs);
  }

  private scheduleReconnect(delayMs: number): void {
    this.reconnectTimer.schedule(delayMs, () => {
      this.reconnectScheduled = false;
      if (!this.disposed) this.connect();
    });
  }

  // ── inbound ──────────────────────────────────────────────

  private handleFrame(event: MessageEvent): void {
    if (this.framesHeld) {
      if (this.heldFrames.length >= SessionAgentSocket.MAX_HELD_FRAMES) {
        this.heldFrames.shift();
      }
      this.heldFrames.push(event);
      return;
    }
    this.routeFrame(event);
  }

  private routeFrame(event: MessageEvent): void {
    routeAgentFrame(event, {
      sessionId: this.sessionId,
      handlers: this.handlers,
      markAllReceivedChunk: () => {
        for (const pending of this.sends.values()) pending.receivedChunk = true;
      },
      settleResolve: messageIds => this.sends.settleResolve(messageIds),
      settleReject: (messageIds, err) => this.sends.settleReject(messageIds, err),
      getActiveTurn: () => this.activeTurn,
      setActiveTurn: turn => {
        this.activeTurn = turn;
      },
      pendingKeys: () => this.sends.keys(),
      flushStopResolvers: () => {
        this.stopResolvers.splice(0).forEach(resolve => resolve());
      },
      getSnapshotSeq: () => this.snapshotSeq,
      setSnapshotSeq: seq => {
        this.snapshotSeq = seq;
      },
      getSnapshotTurnId: () => this.snapshotTurnId,
      setSnapshotTurnId: turnId => {
        this.snapshotTurnId = turnId;
      }
    });
  }
}

/** Generate a protocol `msg_id` (RFC4122 v4 UUID). */
function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
