import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ws.ts uses `emit` from the shared mitt bus. Tests subscribe through the
// same bus (`on`) to observe emitted events.
import { on, off } from '../mitt';
import { useWs, closeWs } from '../ws';

class FakeWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  url: string;
  readyState: number = FakeWebSocket.CONNECTING;
  onopen: ((ev: any) => void) | null = null;
  onmessage: ((ev: any) => void) | null = null;
  onerror: ((ev: any) => void) | null = null;
  onclose: ((ev: any) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
  }

  // Test helper: simulate the browser opening the socket.
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }
  // Test helper: simulate a server message frame.
  message(raw: string) {
    this.onmessage?.({ data: raw } as MessageEvent);
  }
  // Test helper: simulate the socket closing.
  closeFromServer() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({} as CloseEvent);
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  closeWs();
});

describe('resolveWsBaseUrl via useWs connection URL', () => {
  it('connects to ws://host:port/sessions/ws?session_id=default', () => {
    useWs();
    expect(FakeWebSocket.instances).toHaveLength(1);
    const socket = FakeWebSocket.instances[0]!;
    expect(socket.url).toBe('ws://localhost:8080/sessions/ws?session_id=default');
  });

  it('emits ws:connected on open and flips isConnected', () => {
    const events: string[] = [];
    const handler = () => events.push('connected');
    on('ws:connected', handler);

    const { isConnected } = useWs();
    const socket = FakeWebSocket.instances[0]!;
    expect(isConnected.value).toBe(false);

    socket.open();
    expect(isConnected.value).toBe(true);
    expect(events).toEqual(['connected']);
    off('ws:connected', handler);
  });

  it('reuses the open singleton instead of creating a second socket', () => {
    const first = useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    const second = useWs();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(second.ws.value).toBe(first.ws.value);
    expect(second.isConnected.value).toBe(true);
  });
});

describe('message handling', () => {
  it('emits ws:notification for a notification event and ws:message for all frames', () => {
    const notifications: unknown[] = [];
    const messages: unknown[] = [];
    const nHandler = (c: unknown) => notifications.push(c);
    const mHandler = (d: unknown) => messages.push(d);
    on('ws:notification', nHandler);
    on('ws:message', mHandler);

    const { isConnected } = useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    socket.message(JSON.stringify({ event: 'notification', content: 'alert!' }));
    socket.message(JSON.stringify({ event: 'other', content: 'x' }));

    expect(notifications).toEqual(['alert!']);
    expect(messages).toEqual([
      { event: 'notification', content: 'alert!' },
      { event: 'other', content: 'x' }
    ]);
    expect(isConnected.value).toBe(true);

    off('ws:notification', nHandler);
    off('ws:message', mHandler);
  });

  it('ignores malformed JSON frames', () => {
    const notifications: unknown[] = [];
    const nHandler = (c: unknown) => notifications.push(c);
    on('ws:notification', nHandler);

    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    // malformed JSON should be swallowed (no throw/emit)
    expect(() => socket.message('not-json')).not.toThrow();
    expect(notifications).toEqual([]);

    off('ws:notification', nHandler);
  });
});

describe('disconnect and reconnect', () => {
  it('emits ws:disconnected, clears state, and reconnects after 5s', () => {
    vi.useFakeTimers();
    const disconnected: unknown[] = [];
    const dHandler = (c: unknown) => disconnected.push(c);
    on('ws:disconnected', dHandler);
    const onReconnect = vi.fn();

    useWs({ onReconnect });
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    expect(FakeWebSocket.instances).toHaveLength(1);

    socket.closeFromServer();

    expect(disconnected).toEqual([undefined]);
    expect(onReconnect).not.toHaveBeenCalled();

    // Advance 5 seconds: reconnect fires and opens a fresh socket.
    vi.advanceTimersByTime(5000);
    expect(onReconnect).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    off('ws:disconnected', dHandler);
  });
});

describe('reconnect storm guards', () => {
  it('reuses a CONNECTING singleton instead of closing and recreating it', () => {
    // Before the fix: the second useWs() would close the not-yet-handshaked connection and create a new socket,
    // with both sides' onclose scheduling a 5s reconnect -> the two links kept killing each other's connection
    // (backend logs showed disconnect/connect pairs appearing every few seconds)
    const first = useWs(); // FakeWebSocket defaults to readyState=CONNECTING
    const second = useWs();
    const socket = FakeWebSocket.instances[0]!;

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(second.ws.value).toBe(first.ws.value);
    expect(socket.closed).toBe(false);
  });

  it("a superseded socket's late onclose does not schedule a reconnect", () => {
    vi.useFakeTimers();
    useWs(); // socket A
    const a = FakeWebSocket.instances[0]!;
    a.open();

    closeWs(); // wsInstance=null, A is closed (the fake's close does not trigger onclose)
    useWs(); // socket B becomes the new singleton
    const b = FakeWebSocket.instances[1]!;
    b.open();

    // In a real browser, close completion is asynchronous: A's onclose may arrive "late"
    a.closeFromServer();
    vi.advanceTimersByTime(5000);

    // Before the fix: A's late onclose would clear the singleton registration and schedule a reconnect,
    // killing B after 5s and producing a third socket (mutual-kill loop)
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(FakeWebSocket.instances[1]).toBe(b);
    expect(b.readyState).toBe(FakeWebSocket.OPEN);
  });
});

/* ---------------------------------------------------------------------------
 * Application-level heartbeat (ping/pong liveness detection)
 *
 * Constant values are kept in sync with ws.ts: HEARTBEAT_INTERVAL_MS=10000 /
 * PONG_TIMEOUT_MS=5000 / MAX_MISSED_PONGS=2 (literal duplication, avoiding
 * exporting the constants from the implementation just for testing).
 * ------------------------------------------------------------------------- */
const HEARTBEAT_INTERVAL_MS = 10000;
const PONG_TIMEOUT_MS = 5000;

/** Wire format of the server's pong frame (core.py's ping_processor returns {"event": "pong"}) */
const PONG_FRAME = JSON.stringify({ event: 'pong' });

describe('session ws heartbeat', () => {
  it('sends exactly one ping frame on the first interval tick', () => {
    vi.useFakeTimers();
    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS - 1);
    expect(socket.sent).toEqual([]);

    vi.advanceTimersByTime(1);
    expect(socket.sent).toEqual([JSON.stringify({ session_id: 'default', event: 'ping', content: '' })]);
  });

  it('any incoming frame clears the pending pong', () => {
    vi.useFakeTimers();
    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    // First tick sends ping, then a pong frame is received
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    expect(socket.sent).toHaveLength(1);
    socket.message(PONG_FRAME);

    // Cross the pong deadline: a frame was received, so no timeout is judged and the connection stays;
    // the next tick can still send
    vi.advanceTimersByTime(PONG_TIMEOUT_MS);
    expect(socket.closed).toBe(false);

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS - PONG_TIMEOUT_MS);
    expect(socket.sent).toHaveLength(2);
    expect(socket.closed).toBe(false);
  });

  it('malformed frames also clear the pending pong (cleared before JSON parse)', () => {
    vi.useFakeTimers();
    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    expect(socket.sent).toHaveLength(1);

    expect(() => socket.message('not-json')).not.toThrow();
    vi.advanceTimersByTime(PONG_TIMEOUT_MS);
    expect(socket.closed).toBe(false);
  });

  it('one missed pong is tolerated; two consecutive misses force close + events', () => {
    vi.useFakeTimers();
    const heartbeatTimeouts: unknown[] = [];
    const disconnected: unknown[] = [];
    const htHandler = (c: unknown) => heartbeatTimeouts.push(c);
    const dHandler = (c: unknown) => disconnected.push(c);
    on('ws:heartbeat_timeout', htHandler);
    on('ws:disconnected', dHandler);

    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    // tick #1 (t=10s) sends ping; t=15s pong deadline -> 1 consecutive timeout
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    vi.advanceTimersByTime(PONG_TIMEOUT_MS);
    expect(socket.closed).toBe(false);

    // tick #2 (t=20s): a single timeout is tolerated; the second ping frame is still sent
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS - PONG_TIMEOUT_MS);
    expect(socket.sent).toHaveLength(2);
    expect(socket.closed).toBe(false);

    // t=25s second deadline: 2 consecutive timeouts -> broadcast ws:heartbeat_timeout and force close()
    vi.advanceTimersByTime(PONG_TIMEOUT_MS);
    expect(socket.closed).toBe(true);
    expect(heartbeatTimeouts).toHaveLength(1);

    // Browser semantics: after close(), onclose fires asynchronously — simulate with closeFromServer,
    // verifying ws:disconnected is dispatched by the existing onclose (the heartbeat logic itself
    // neither re-broadcasts nor reconnects)
    socket.closeFromServer();
    expect(disconnected).toEqual([undefined]);

    off('ws:heartbeat_timeout', htHandler);
    off('ws:disconnected', dHandler);
  });

  it('pong received before the deadline prevents the close', () => {
    vi.useFakeTimers();
    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    expect(socket.sent).toHaveLength(1);

    // Pong received 2s before the deadline: cancels the timeout check, never judged dead
    vi.advanceTimersByTime(PONG_TIMEOUT_MS - 2000);
    socket.message(PONG_FRAME);
    vi.advanceTimersByTime(2000);
    expect(socket.closed).toBe(false);
    expect(socket.sent).toHaveLength(1);

    // Heartbeat continues at its original cadence (next frame at t=20s)
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS - PONG_TIMEOUT_MS);
    expect(socket.sent).toHaveLength(2);
    expect(socket.closed).toBe(false);
  });

  it('leaves no timers behind after close (no sends, no errors across minutes)', () => {
    vi.useFakeTimers();
    useWs();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    expect(socket.sent).toHaveLength(1);

    socket.closeFromServer();
    // onclose clears the heartbeat interval; at this point only the 5s reconnect setTimeout remains
    expect(vi.getTimerCount()).toBe(1);

    // Cross the reconnect + several minutes: the new socket has no heartbeat while not open; the old socket sends nothing more
    vi.advanceTimersByTime(5000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const reconnected = FakeWebSocket.instances[1]!;
    vi.advanceTimersByTime(10 * 60 * 1000);

    expect(vi.getTimerCount()).toBe(0);
    expect(socket.sent).toHaveLength(1);
    expect(reconnected.sent).toEqual([]);
  });
});
