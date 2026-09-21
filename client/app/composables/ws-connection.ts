/**
 * Shared WebSocket connection lifecycle.
 *
 * Owns one socket slot plus its reconnect schedule, so the session-push channel
 * (`ws.ts::useWs`) and the subagent-push channel (`ws.ts::useSubagentWs`) no
 * longer duplicate connect / close / superseded-socket / fixed-delay-reconnect
 * handling.
 *
 * The base expresses the common lifecycle — connect, reconnect scheduling,
 * optional application-layer ping/pong heartbeat, and cleanup — while each
 * channel keeps its own semantics through the hooks:
 * - the session channel starts a heartbeat (ping every 10s, 2 consecutive 5s
 *   pong timeouts declare the socket dead) and tracks `everConnected` for its
 *   `ws:reconnected` emit; the subagent channel has no heartbeat and tracks its
 *   `ready` handshake in module state instead;
 * - `bridge/agent-socket.ts` stays its own class: its reconnect decision is
 *   resumption-aware (mid-stream sends are rejected, pre-chunk sends are
 *   re-sent on open) and its socket is not a module singleton, so folding it
 *   into this base would mean re-implementing that policy as hooks.
 *
 * The socket slot is the source of truth for "is this socket still current":
 * the handlers of a superseded or disposed connection bail out, so a late
 * onclose can never clear a live singleton or schedule a competing reconnect.
 */

/** Application-layer ping/pong liveness check. */
export interface WsHeartbeat {
  /** Ping send interval (ms). */
  intervalMs: number;
  /** Pong timeout window after a ping (ms). */
  timeoutMs: number;
  /** Consecutive timeouts that declare the socket dead. */
  maxMissed: number;
  /** Build the ping frame (serialized by the base). */
  frame: () => unknown;
  /** Called when `maxMissed` consecutive pongs were missed, before the close. */
  onTimeout: () => void;
}

/** Per-connection behavior and channel-specific state. */
export interface WsConnectionOptions {
  /** Socket URL. */
  url: string;
  /** Fixed delay before an unexpected close reconnects (ms). */
  reconnectDelayMs: number;
  /** Extra work on open (after the socket slot is current and the heartbeat is armed). */
  onOpen?: () => void;
  /** Frame dispatch for the live socket (every frame, before any parsing). */
  onFrame?: (event: MessageEvent) => void;
  /** The live socket dropped (its timers are already cleared). */
  onClose?: () => void;
  /** Reconnect tick: invoked just before the new connect attempt. */
  onReconnect?: () => void;
  /** Optional heartbeat (session channel only). */
  heartbeat?: WsHeartbeat;
}

/**
 * One WebSocket with a fixed-delay reconnect schedule.
 *
 * The caller owns the instance (a module singleton per channel), so an explicit
 * replacement disposes the previous instance and thereby cancels its pending
 * reconnect.
 */
export class WsConnection {
  private socketInstance: WebSocket | null = null;

  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;

  private pongTimeoutTimer: ReturnType<typeof setTimeout> | null = null;

  private pendingPong = false;

  private missedPongs = 0;

  private disposed = false;

  constructor(private readonly options: WsConnectionOptions) {}

  /** The current socket, or null while closed/reconnecting. */
  get socket(): WebSocket | null {
    return this.socketInstance;
  }

  /** Whether the current socket is OPEN. */
  get isOpen(): boolean {
    return this.socketInstance !== null && this.socketInstance.readyState === WebSocket.OPEN;
  }

  /** Whether the current socket is still handshaking. */
  get isConnecting(): boolean {
    return this.socketInstance !== null && this.socketInstance.readyState === WebSocket.CONNECTING;
  }

  /**
   * Open a socket. Any previous socket of this instance is closed first, and a
   * pending auto-reconnect is cancelled (an explicit connect supersedes it).
   * A disposed instance never reconnects.
   */
  connect(): void {
    if (this.disposed) return;
    this.clearReconnectTimer();
    if (this.socketInstance) {
      this.socketInstance.close();
      this.socketInstance = null;
    }

    const socket = new WebSocket(this.options.url);
    this.socketInstance = socket;

    socket.onopen = () => {
      if (this.socketInstance !== socket) return;
      this.pendingPong = false;
      this.missedPongs = 0;
      this.startHeartbeat(socket);
      this.options.onOpen?.();
    };

    socket.onmessage = (event: MessageEvent) => {
      if (this.socketInstance !== socket) return;
      // Receiving any frame (including pong) proves the server's event loop is
      // alive: clear pending/counters and cancel this round's timeout check
      // before the channel dispatches the frame.
      this.pendingPong = false;
      this.missedPongs = 0;
      this.clearPongTimeout();
      this.options.onFrame?.(event);
    };

    socket.onclose = () => {
      // A socket superseded by a newer connect/dispose must not clear the live
      // slot or schedule a competing reconnect.
      if (this.socketInstance !== socket) return;
      this.stopHeartbeat();
      this.socketInstance = null;
      this.options.onClose?.();
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this.options.onReconnect?.();
        this.connect();
      }, this.options.reconnectDelayMs);
    };

    socket.onerror = () => {
      // onclose fires automatically after onerror; reconnection is handled there
    };
  }

  /**
   * Close the socket and stop reconnecting. Idempotent; the instance is spent
   * (a replacement channel is a new instance) so late socket events are ignored.
   */
  dispose(): void {
    this.disposed = true;
    this.clearReconnectTimer();
    this.stopHeartbeat();
    const socket = this.socketInstance;
    this.socketInstance = null;
    socket?.close();
  }

  // ── heartbeat ────────────────────────────────────────────

  /**
   * Start the heartbeat interval (called on every onopen).
   * Defensively stops any previous timers first, so the previous connection's
   * timers never survive into the new connection cycle.
   * @param socket The socket this heartbeat belongs to.
   */
  private startHeartbeat(socket: WebSocket): void {
    this.stopHeartbeat();
    const heartbeat = this.options.heartbeat;
    if (!heartbeat) return;
    this.heartbeatTimer = setInterval(() => this.heartbeatTick(socket), heartbeat.intervalMs);
  }

  /**
   * Single heartbeat tick: send a ping frame and schedule the timeout check when
   * OPEN and no ping is pending.
   * @param socket The socket this heartbeat belongs to.
   */
  private heartbeatTick(socket: WebSocket): void {
    const heartbeat = this.options.heartbeat;
    // Connection unavailable (closing/closed) or the previous ping is still
    // awaiting a pong: skip this tick; the pending timeout callback decides.
    if (!heartbeat || socket.readyState !== WebSocket.OPEN || this.pendingPong) return;

    socket.send(JSON.stringify(heartbeat.frame()));
    this.pendingPong = true;

    // The timeout is measured from the actual send moment: both the send and the
    // check run in timers, which a throttled background tab delays equally, so
    // throttling cannot produce false positives.
    this.pongTimeoutTimer = setTimeout(() => {
      this.pongTimeoutTimer = null;
      if (!this.pendingPong) return;

      this.missedPongs += 1;
      // Release the pending flag so the next tick can ping again: a single lost
      // pong is most likely network jitter — suspicious but not fatal.
      this.pendingPong = false;

      if (this.missedPongs >= heartbeat.maxMissed) {
        // Declare the connection dead; reconnection is left to the onclose path.
        heartbeat.onTimeout();
        socket.close();
      }
    }, heartbeat.timeoutMs);
  }

  /** Clear the pong timeout-check handle. */
  private clearPongTimeout(): void {
    if (this.pongTimeoutTimer !== null) {
      clearTimeout(this.pongTimeoutTimer);
      this.pongTimeoutTimer = null;
    }
  }

  /** Stop the heartbeat: clear the interval and any pending pong timeout check. */
  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    this.clearPongTimeout();
  }

  /** Cancel a pending auto-reconnect. */
  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}
