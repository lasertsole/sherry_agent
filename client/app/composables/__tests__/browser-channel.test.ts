/**
 * The browser panel's live channel: URL (session + token + ticket), frame
 * dispatch and the "only send while OPEN" contract.
 *
 * The transport (`WsConnection`) has its own suite; here the fake WebSocket
 * drives a channel end to end so the panel's frame handling is pinned: a
 * `ready` snapshot, `page` updates, `frame` payloads (with the device size),
 * `error` text, and a reconnect that keeps the same handlers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { BrowserChannel } from '../browser-channel';

class FakeWebSocket {
  static OPEN = 1;

  static CONNECTING = 0;

  static CLOSED = 3;

  static instances: FakeWebSocket[] = [];

  url: string;

  readyState: number = FakeWebSocket.CONNECTING;

  onopen: ((ev: unknown) => void) | null = null;

  onmessage: ((ev: unknown) => void) | null = null;

  onerror: ((ev: unknown) => void) | null = null;

  onclose: ((ev: unknown) => void) | null = null;

  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  /**
   * Deliver one server frame.
   * @param frame
   */
  message(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }

  closeFromServer() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({} as CloseEvent);
  }
}

function handlers() {
  return {
    onReady: vi.fn(),
    onPage: vi.fn(),
    onFrame: vi.fn(),
    onError: vi.fn(),
    onOpen: vi.fn(),
    onClose: vi.fn()
  };
}

describe('BrowserChannel', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('connects to the session channel with credentials attached', () => {
    const hooks = handlers();
    const channel = new BrowserChannel('sid-1', hooks);
    channel.connect();

    const socket = FakeWebSocket.instances[0]!;
    expect(socket.url).toContain('/browser/ws?session_id=sid-1');
    socket.open();
    expect(hooks.onOpen).toHaveBeenCalled();
    channel.dispose();
  });

  it('routes every frame type to its handler', () => {
    const hooks = handlers();
    const channel = new BrowserChannel('sid-1', hooks);
    channel.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    socket.message({
      event: 'ready',
      enabled: true,
      running: false,
      page: null,
      url: '',
      can_back: false,
      can_forward: false
    });
    expect(hooks.onReady).toHaveBeenCalledWith(expect.objectContaining({ enabled: true, page: null }));

    socket.message({
      event: 'page',
      page: 'p1',
      url: 'https://x.test',
      can_back: true,
      can_forward: false
    });
    expect(hooks.onPage).toHaveBeenCalledWith(
      expect.objectContaining({ page: 'p1', url: 'https://x.test', can_back: true })
    );

    socket.message({ event: 'frame', data: 'AAA', width: 393, height: 852 });
    expect(hooks.onFrame).toHaveBeenCalledWith({ data: 'AAA', width: 393, height: 852 });

    // Missing metadata degrades to nulls (the panel falls back to its viewport).
    socket.message({ event: 'frame', data: 'BBB' });
    expect(hooks.onFrame).toHaveBeenLastCalledWith({ data: 'BBB', width: null, height: null });

    socket.message({ event: 'error', message: 'unknown ref' });
    expect(hooks.onError).toHaveBeenCalledWith('unknown ref');

    // Unknown frames and malformed JSON are ignored, not fatal.
    socket.message({ event: 'pong' });
    socket.message(undefined);
    expect(hooks.onError).toHaveBeenCalledTimes(1);
    channel.dispose();
  });

  it('only sends while the socket is OPEN', () => {
    const hooks = handlers();
    const channel = new BrowserChannel('sid-1', hooks);
    channel.connect();
    const socket = FakeWebSocket.instances[0]!;

    expect(channel.send({ event: 'ping' })).toBe(false);
    expect(socket.sent).toEqual([]);

    socket.open();
    expect(channel.isOpen).toBe(true);
    expect(channel.send({ event: 'nav', url: 'https://x.test' })).toBe(true);
    expect(JSON.parse(socket.sent[0]!)).toEqual({ event: 'nav', url: 'https://x.test' });

    socket.closeFromServer();
    expect(channel.isOpen).toBe(false);
    channel.dispose();
  });

  it('reconnects after a drop and reports it', async () => {
    vi.useFakeTimers();
    const hooks = handlers();
    const channel = new BrowserChannel('sid-1', hooks);
    channel.connect();
    FakeWebSocket.instances[0]!.open();

    FakeWebSocket.instances[0]!.closeFromServer();
    expect(hooks.onClose).toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(2100);
    expect(FakeWebSocket.instances).toHaveLength(2);
    // A disposed channel stops reconnecting.
    channel.dispose();
    FakeWebSocket.instances[1]!.closeFromServer();
    await vi.advanceTimersByTimeAsync(2100);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
