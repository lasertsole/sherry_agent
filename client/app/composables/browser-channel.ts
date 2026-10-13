/**
 * The browser panel's live channel (CDP mode).
 *
 * One socket per panel instance against `/browser/ws` (see
 * `server/trigger/ws/browser_ws.py`): JPEG screencast frames come in, and
 * navigation / input / viewport intent goes out. The panel never touches the
 * CDP port — the manager performs every browser call in-process.
 *
 * The transport is the shared `WsConnection` (fixed-delay reconnect + a
 * superseded-socket guard), so an idle panel that loses the backend keeps
 * retrying and re-renders when a fresh frame arrives; the server re-issues
 * `startScreencast` on every `watch`.
 *
 * The transport symbols (`WsConnection`, `WS_BASE_URL`, `withGatewayToken`,
 * `appendWsTicket`, `prepareWsTicket`) are auto-imported: a top-level
 * `app/composables/*.ts` must not relatively import a sibling.
 *
 * @module composables/browser-channel
 */

/** One screencast frame: a base64 JPEG plus the device size when the server reports it. */
export interface BrowserFrame {
  data: string;
  width: number | null;
  height: number | null;
}

/** The opening snapshot: the feature switch, the browser's state and the watched page. */
export interface BrowserReadyFrame {
  enabled: boolean;
  running: boolean;
  /** The page the channel is watching (null until the first navigation). */
  page: string | null;
  url: string;
  title: string;
  can_back: boolean;
  can_forward: boolean;
  /** "page" for the page itself, "devtools" for the inspector frontend. */
  kind: string;
}

/** A page frame: sent after every navigation, history move and watch switch. */
export interface BrowserPageFrame {
  page: string | null;
  url: string;
  title: string;
  can_back: boolean;
  can_forward: boolean;
  /** "page" for the page itself, "devtools" for the inspector frontend. */
  kind: string;
}

/** What the panel does with each frame type. */
export interface BrowserChannelHandlers {
  onReady: (frame: BrowserReadyFrame) => void;
  onPage: (frame: BrowserPageFrame) => void;
  onFrame: (frame: BrowserFrame) => void;
  /** A refused command (never fatal): the panel shows it and stays connected. */
  onError: (message: string) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

/** Structural view of the frames the server sends (everything else is ignored). */
interface RawFrame {
  event?: string;
  enabled?: boolean;
  running?: boolean;
  page?: string | null;
  url?: string;
  title?: string;
  can_back?: boolean;
  can_forward?: boolean;
  kind?: string;
  data?: string;
  width?: number | null;
  height?: number | null;
  message?: string;
}

/**
 * One panel's channel to the session's browser.
 *
 * Create it per mounted panel instance, `connect()` once and `dispose()` on
 * unmount: a disposed channel never reconnects, and a new mount opens a fresh
 * frame stream (the right sidebar unmounts inactive tabs by design).
 */
export class BrowserChannel {
  private readonly connection: WsConnection;

  /**
   * Wire one panel instance to a session's browser.
   *
   * @param sessionId The session whose browser pages this panel watches.
   * @param handlers Frame callbacks (one per frame type).
   */
  constructor(
    private readonly sessionId: string,
    private readonly handlers: BrowserChannelHandlers
  ) {
    this.connection = new WsConnection({
      url: () =>
        appendWsTicket(withGatewayToken(`${WS_BASE_URL}/browser/ws?session_id=${encodeURIComponent(this.sessionId)}`)),
      beforeConnect: prepareWsTicket,
      reconnectDelayMs: 2000,
      onOpen: () => handlers.onOpen?.(),
      onClose: () => handlers.onClose?.(),
      onFrame: event => this.dispatch(event)
    });
  }

  /**
   * Whether the socket is OPEN right now (a command needs it to be).
   *
   * @returns True while a `send` would be accepted.
   */
  get isOpen(): boolean {
    return this.connection.isOpen;
  }

  /** Open the socket (and keep reconnecting after a drop). */
  connect(): void {
    this.connection.connect();
  }

  /** Close the socket and stop reconnecting (idempotent). */
  dispose(): void {
    this.connection.dispose();
  }

  /**
   * Send one command frame.
   * @param payload The command (`{event: …}` shape the server expects).
   * @returns True when the socket took it; false when not connected.
   */
  send(payload: Record<string, unknown>): boolean {
    const socket = this.connection.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(payload));
    return true;
  }

  /**
   * Route one raw server frame to its handler.
   * @param event
   */
  private dispatch(event: MessageEvent): void {
    let payload: RawFrame;
    try {
      payload = JSON.parse(String(event.data)) as RawFrame;
    } catch {
      return; // a malformed frame is not this channel's problem
    }
    if (!payload || typeof payload.event !== 'string') return;
    switch (payload.event) {
      case 'ready':
        this.handlers.onReady({
          enabled: payload.enabled === true,
          running: payload.running === true,
          page: payload.page ?? null,
          url: String(payload.url ?? ''),
          title: String(payload.title ?? ''),
          can_back: payload.can_back === true,
          can_forward: payload.can_forward === true,
          kind: String(payload.kind ?? 'page')
        });
        break;
      case 'page':
        this.handlers.onPage({
          page: payload.page ?? null,
          url: String(payload.url ?? ''),
          title: String(payload.title ?? ''),
          can_back: payload.can_back === true,
          can_forward: payload.can_forward === true,
          kind: String(payload.kind ?? 'page')
        });
        break;
      case 'frame':
        this.handlers.onFrame({
          data: String(payload.data ?? ''),
          width: typeof payload.width === 'number' ? payload.width : null,
          height: typeof payload.height === 'number' ? payload.height : null
        });
        break;
      case 'error':
        this.handlers.onError(String(payload.message ?? 'browser error'));
        break;
      default:
        break; // pong and future frames need no handler
    }
  }
}
