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
  it('connects to ws://host:port/sessions/ws?session_id=main', () => {
    useWs();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toBe(
      'ws://localhost:8080/sessions/ws?session_id=main',
    );
  });

  it('emits ws:connected on open and flips isConnected', () => {
    const events: string[] = [];
    const handler = () => events.push('connected');
    on('ws:connected', handler);

    const { isConnected } = useWs();
    const socket = FakeWebSocket.instances[0];
    expect(isConnected.value).toBe(false);

    socket.open();
    expect(isConnected.value).toBe(true);
    expect(events).toEqual(['connected']);
    off('ws:connected', handler);
  });

  it('reuses the open singleton instead of creating a second socket', () => {
    const first = useWs();
    FakeWebSocket.instances[0].open();

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
    const socket = FakeWebSocket.instances[0];
    socket.open();

    socket.message(JSON.stringify({ event: 'notification', content: 'alert!' }));
    socket.message(JSON.stringify({ event: 'other', content: 'x' }));

    expect(notifications).toEqual(['alert!']);
    expect(messages).toEqual([
      { event: 'notification', content: 'alert!' },
      { event: 'other', content: 'x' },
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
    const socket = FakeWebSocket.instances[0];
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
    const socket = FakeWebSocket.instances[0];
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
