import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { WsConnection } from '../ws-connection';

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

  /** Test helper: simulate the browser opening the socket. */
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  /**
   * Test helper: simulate a server frame.
   * @param raw
   */
  message(raw: unknown) {
    this.onmessage?.({ data: typeof raw === 'string' ? raw : JSON.stringify(raw) } as MessageEvent);
  }

  /** Test helper: simulate the socket closing. */
  closeFromServer() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({} as CloseEvent);
  }
}

function makeConnection(overrides: Partial<ConstructorParameters<typeof WsConnection>[0]> = {}) {
  const hooks = {
    onOpen: vi.fn(),
    onFrame: vi.fn(),
    onClose: vi.fn(),
    onReconnect: vi.fn()
  };
  const connection = new WsConnection({
    url: 'ws://localhost:8080/test/ws',
    reconnectDelayMs: 5000,
    ...hooks,
    ...overrides
  });
  return { connection, hooks };
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('WsConnection lifecycle', () => {
  it('opens a socket with the configured url and reports its state', () => {
    const { connection } = makeConnection();

    expect(connection.socket).toBeNull();
    expect(connection.isOpen).toBe(false);
    expect(connection.isConnecting).toBe(false);

    connection.connect();

    const socket = FakeWebSocket.instances[0]!;
    expect(socket.url).toBe('ws://localhost:8080/test/ws');
    expect(connection.socket).toBe(socket);
    expect(connection.isConnecting).toBe(true);
    expect(connection.isOpen).toBe(false);

    socket.open();
    expect(connection.isOpen).toBe(true);
  });

  it('runs the onOpen hook after the socket opened', () => {
    const { connection, hooks } = makeConnection();
    connection.connect();

    FakeWebSocket.instances[0]!.open();

    expect(hooks.onOpen).toHaveBeenCalledTimes(1);
  });

  it('dispatches frames of the live socket and drops frames of a superseded one', () => {
    const { connection, hooks } = makeConnection();
    connection.connect();
    const first = FakeWebSocket.instances[0]!;
    first.open();

    first.message({ event: 'notification' });
    expect(hooks.onFrame).toHaveBeenCalledTimes(1);

    connection.connect(); // supersedes `first`
    const second = FakeWebSocket.instances[1]!;
    second.open();

    first.message({ event: 'stale' });
    second.message({ event: 'fresh' });

    expect(hooks.onFrame).toHaveBeenCalledTimes(2);
    expect(hooks.onFrame).toHaveBeenLastCalledWith(
      expect.objectContaining({ data: JSON.stringify({ event: 'fresh' }) })
    );
  });

  it('reconnects with the fixed delay after an unexpected close and runs onClose first', () => {
    vi.useFakeTimers();
    const { connection, hooks } = makeConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    socket.closeFromServer();

    expect(hooks.onClose).toHaveBeenCalledTimes(1);
    expect(hooks.onReconnect).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(4999);
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(hooks.onReconnect).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(connection.socket).toBe(FakeWebSocket.instances[1]);
  });

  it('cancels the pending reconnect on dispose and ignores the late onclose', () => {
    vi.useFakeTimers();
    const { connection, hooks } = makeConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    socket.closeFromServer();
    expect(vi.getTimerCount()).toBe(1);

    connection.dispose();
    expect(vi.getTimerCount()).toBe(0);
    expect(connection.socket).toBeNull();

    // A late onclose of the disposed socket must not re-arm anything.
    hooks.onClose.mockClear();
    socket.closeFromServer();
    vi.advanceTimersByTime(60000);

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(hooks.onClose).not.toHaveBeenCalled();
  });

  it('closes the live socket on dispose', () => {
    const { connection } = makeConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    connection.dispose();

    expect(socket.closed).toBe(true);
    expect(connection.socket).toBeNull();
  });

  it('never reconnects after dispose (a spent instance stays closed)', () => {
    vi.useFakeTimers();
    const { connection } = makeConnection();
    connection.connect();
    FakeWebSocket.instances[0]!.open();

    connection.dispose();
    connection.connect();

    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe('WsConnection heartbeat', () => {
  function makeHeartbeatConnection() {
    const onTimeout = vi.fn();
    const { connection, hooks } = makeConnection({
      heartbeat: {
        intervalMs: 10000,
        timeoutMs: 5000,
        maxMissed: 2,
        frame: () => ({ event: 'ping' }),
        onTimeout
      }
    });
    return { connection, hooks, onTimeout };
  }

  it('sends one ping frame per interval tick while open', () => {
    vi.useFakeTimers();
    const { connection } = makeHeartbeatConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(9999);
    expect(socket.sent).toEqual([]);

    vi.advanceTimersByTime(1);
    expect(socket.sent).toEqual([JSON.stringify({ event: 'ping' })]);
  });

  it('clears the pending pong when any frame arrives', () => {
    vi.useFakeTimers();
    const { connection, onTimeout } = makeHeartbeatConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(10000); // ping sent
    socket.message({ event: 'pong' }); // frame arrives before the deadline
    vi.advanceTimersByTime(5000);

    expect(onTimeout).not.toHaveBeenCalled();
    expect(socket.closed).toBe(false);

    // The heartbeat keeps its cadence: the next tick pings again.
    vi.advanceTimersByTime(5000);
    expect(socket.sent).toHaveLength(2);
  });

  it('declares the socket dead after maxMissed consecutive pongs and closes it', () => {
    vi.useFakeTimers();
    const { connection, onTimeout } = makeHeartbeatConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(10000); // tick #1 ping
    vi.advanceTimersByTime(5000); // timeout #1: tolerated
    expect(onTimeout).not.toHaveBeenCalled();
    expect(socket.closed).toBe(false);

    vi.advanceTimersByTime(5000); // tick #2 ping
    expect(socket.sent).toHaveLength(2);
    vi.advanceTimersByTime(5000); // timeout #2 -> dead

    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(socket.closed).toBe(true);
  });

  it('leaves no timers behind after the connection closes', () => {
    vi.useFakeTimers();
    const { connection } = makeHeartbeatConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    vi.advanceTimersByTime(10000);

    socket.closeFromServer();
    // Only the reconnect timer remains.
    expect(vi.getTimerCount()).toBe(1);

    vi.advanceTimersByTime(5000);
    const reconnected = FakeWebSocket.instances[1]!;
    vi.advanceTimersByTime(10 * 60 * 1000);

    expect(vi.getTimerCount()).toBe(0);
    expect(socket.sent).toHaveLength(1);
    expect(reconnected.sent).toEqual([]);
  });

  it('runs without a heartbeat when none is configured', () => {
    vi.useFakeTimers();
    const { connection } = makeConnection();
    connection.connect();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    vi.advanceTimersByTime(60 * 1000);

    expect(socket.sent).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });
});
