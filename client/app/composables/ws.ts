/**
 * WebSocket connection management and message listening
 *
 * WebSocket connection management:
 * - Creates and maintains a WebSocket connection
 * - Listens for messages continuously in the background and dispatches events
 *   via the mitt event bus
 * - The /sessions/ws channel has a built-in application-layer ping/pong
 *   heartbeat (10s interval; 2 consecutive 5s timeouts declare the connection dead)
 *
 * @module ws
 */

import { ref, type Ref } from 'vue';
import { emit } from './mitt';

/** Session ID (currently fixed to "default") */
const SESSION_ID = 'default';

/** WebSocket singleton reference */
let wsInstance: WebSocket | null = null;

/* ---------------------------------------------------------------------------
 * Application-layer heartbeat (ping/pong liveness check) — applies only to the
 * /sessions/ws session channel above
 *
 * Every HEARTBEAT_INTERVAL_MS a { event: 'ping' } frame is sent, and the server
 * replies with {"event":"pong"}; after a ping is sent, receiving any frame
 * within PONG_TIMEOUT_MS counts as alive. Only after MAX_MISSED_PONGS
 * consecutive pong timeouts is the connection declared dead and actively
 * close()d (close triggers the existing onclose -> broadcast ws:disconnected +
 * 5s auto-reconnect; reconnect logic is not duplicated here).
 * ------------------------------------------------------------------------- */

/** Heartbeat send interval (milliseconds) */
const HEARTBEAT_INTERVAL_MS = 10000;

/** Pong timeout window: if no server frame arrives within this duration after a ping is sent, count one timeout (milliseconds) */
const PONG_TIMEOUT_MS = 5000;

/** Consecutive pong timeout threshold: the connection is declared dead only when this count is reached */
const MAX_MISSED_PONGS = 2;

/** Heartbeat interval handle (module-level: cleaned up uniformly by onclose / closeWs, preventing leaks across reconnect cycles) */
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

/** Timeout-check handle for the current ping */
let pongTimeoutTimer: ReturnType<typeof setTimeout> | null = null;

/** A ping has been sent and no reply frame has been received yet */
let pendingPong = false;

/** Consecutive pong timeout count (reset to zero when any frame arrives) */
let missedPongs = 0;

/** Clear the pong timeout-check handle */
function clearPongTimeout(): void {
  if (pongTimeoutTimer !== null) {
    clearTimeout(pongTimeoutTimer);
    pongTimeoutTimer = null;
  }
}

/** Stop the heartbeat: clear the interval and any pending pong timeout check (called on reconnect cycles / closeWs) */
function stopHeartbeat(): void {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  clearPongTimeout();
}

/**
 * Start the heartbeat interval (called on every onopen).
 * Defensively calls stopHeartbeat first, ensuring the previous connection's
 * timers never survive into the new connection cycle.
 */
function startHeartbeat(socket: WebSocket): void {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => heartbeatTick(socket), HEARTBEAT_INTERVAL_MS);
}

/** Single heartbeat tick: send a ping frame and schedule the timeout check when OPEN and no ping is pending */
function heartbeatTick(socket: WebSocket): void {
  // Connection unavailable (closing/closed) or the previous ping frame is still
  // awaiting a pong: skip this tick; the pending timeout callback will handle
  // the counting/decision
  if (socket.readyState !== WebSocket.OPEN || pendingPong) return;

  socket.send(JSON.stringify({ session_id: SESSION_ID, event: 'ping', content: '' }));
  pendingPong = true;

  // The timeout is measured from the "actual send moment": the send happens
  // inside the interval callback and the timeout check also runs inside a
  // timer callback; background browser tabs throttle both kinds of timers
  // equally (delaying the send and the check by the same amount), so
  // throttling does not produce false positives.
  pongTimeoutTimer = setTimeout(() => {
    pongTimeoutTimer = null;
    if (!pendingPong) return;

    missedPongs += 1;
    // Release the pending flag so the next heartbeat tick can send a ping again:
    // a single lost pong is most likely network jitter — suspicious but not fatal
    pendingPong = false;

    if (missedPongs >= MAX_MISSED_PONGS) {
      // Two consecutive timeouts: declare the connection dead. Only broadcast
      // the event and force close(); reconnection is left to the existing
      // 5s auto-reconnect logic in onclose
      emit('ws:heartbeat_timeout', undefined);
      socket.close();
    }
  }, PONG_TIMEOUT_MS);
}

/**
 * Parse the host and port parts from a ws:// or wss:// URL
 *
 * Extracts the host and port from VITE_API_BACK_URL (e.g. http://localhost:8080)
 * and builds the corresponding WebSocket URL.
 *
 * @param apiBaseUrl HTTP base URL
 * @returns WebSocket base URL (ws://host:port)
 */
function resolveWsBaseUrl(apiBaseUrl: string): string {
  // Replace the protocol: http:// -> ws://, https:// -> wss://
  const wsUrl = apiBaseUrl.replace(/^https?:\/\//, match => (match === 'http://' ? 'ws://' : 'wss://'));
  // Strip the trailing /
  return wsUrl.replace(/\/+$/, '');
}

/**
 * Create and obtain the WebSocket connection (singleton)
 *
 * Equivalent of @st.cache_resource: module-level singleton + connection state
 * management
 *
 * @param {{ onReconnect?: () => void }} [options] Optional connection-restored callback
 * @returns {{ ws: Ref<WebSocket | null>, isConnected: Ref<boolean> }}
 */
export function useWs(options?: { onReconnect?: () => void }): {
  ws: Ref<WebSocket | null>;
  isConnected: Ref<boolean>;
} {
  const ws: Ref<WebSocket | null> = ref(null);
  const isConnected: Ref<boolean> = ref(false);

  if (wsInstance && wsInstance.readyState === WebSocket.OPEN) {
    ws.value = wsInstance;
    isConnected.value = true;
    return { ws, isConnected };
  }

  const baseUrl = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const wsBase = resolveWsBaseUrl(baseUrl);
  const wsUrl = `${wsBase}/sessions/ws?session_id=${SESSION_ID}`;

  function connect(): void {
    // An existing connection is still handshaking: reuse it directly, never
    // close and rebuild — otherwise multiple callers (connection.ts startup +
    // NotificationDialog mount) would close each other's not-yet-finished
    // connections, and both sides' onclose would schedule 5s reconnects,
    // creating a "reconnect storm"
    if (wsInstance && wsInstance.readyState === WebSocket.CONNECTING) {
      ws.value = wsInstance;
      return;
    }

    // Close the old connection
    if (wsInstance) {
      wsInstance.close();
      wsInstance = null;
    }

    const socket = new WebSocket(wsUrl);
    wsInstance = socket;
    ws.value = socket;

    socket.onopen = () => {
      isConnected.value = true;
      emit('ws:connected', undefined);

      // Reset heartbeat counters and start the heartbeat timer (counting
      // restarts from scratch on every reconnect)
      pendingPong = false;
      missedPongs = 0;
      startHeartbeat(socket);
    };

    socket.onmessage = (event: MessageEvent) => {
      // Receiving any frame (including pong) proves the server's event loop is
      // alive: first clear pending/counters and cancel this round's timeout
      // check, then do the original event dispatch
      pendingPong = false;
      missedPongs = 0;
      clearPongTimeout();

      try {
        const data = JSON.parse(event.data);
        const eventType: string = data.event ?? '';
        const content: unknown = data.content ?? '';

        if (eventType === 'notification') {
          // Dispatch the notification event for components to listen to
          emit('ws:notification', content);
        }

        // Pass through the raw event
        emit('ws:message', data);
      } catch {
        // JSON parse failed; ignore this message
      }
    };

    socket.onclose = () => {
      // When this socket has been superseded by a newer connection (rebuilt by
      // another caller / reopened after closeWs), it must not schedule a
      // reconnect — otherwise the old link's timers would kill the new
      // connection, creating a cycle of mutual kills
      if (wsInstance !== socket) return;

      // Clean up heartbeat timers first: connect() closes the old connection,
      // so old timers must not survive across reconnect cycles
      stopHeartbeat();

      isConnected.value = false;
      ws.value = null;
      wsInstance = null;
      emit('ws:disconnected', undefined);

      // Auto-reconnect (after 5 seconds)
      setTimeout(() => {
        options?.onReconnect?.();
        connect();
      }, 5000);
    };

    socket.onerror = () => {
      // onclose fires automatically after onerror; reconnection is handled by onclose
    };
  }

  connect();

  return { ws, isConnected };
}

/**
 * Manually close the WebSocket connection (for cleanup)
 */
export function closeWs(): void {
  // The heartbeat timer is a module-level handle; clean it up together with the
  // singleton close (a safety net beyond onclose, to prevent leaks)
  stopHeartbeat();
  pendingPong = false;
  missedPongs = 0;
  if (wsInstance) {
    wsInstance.close();
    wsInstance = null;
  }
}

/**
 * Whether the session push WebSocket (/sessions/ws) singleton is currently OPEN.
 *
 * Lets external modules such as connection.ts read the singleton's real-time
 * connection state (instead of each maintaining its own mirrored copy).
 */
export function isSessionWsOpen(): boolean {
  return wsInstance !== null && wsInstance.readyState === WebSocket.OPEN;
}

/* ---------------------------------------------------------------------------
 * Subagent (subtask) real-time push WebSocket (/subagents/ws)
 *
 * A connection independent from the `/sessions/ws` session push channel above.
 * When a subtask is spawned / ended, the backend pushes that subtask's run
 * record to the frontend via the two wire events `subagent_spawned` /
 * `subagent_ended`; once the connection is established, the server first sends
 * a `ready` welcome frame.
 *
 * After receiving an event, the frontend writes each frame's `data` (the full
 * record including run_id) to IndexedDB, so the background task list can update
 * in real time using Dexie as the authoritative data source. Automatic
 * reconnection on disconnect is supported as well.
 * ------------------------------------------------------------------------- */

/** Subagent WebSocket singleton reference */
let subagentWsInstance: WebSocket | null = null;

/** Whether the subagent connection is ready (ready frame received) */
let subagentReady = false;

/**
 * Create and obtain the subagent real-time push WebSocket connection (singleton)
 *
 * @param {{ onReconnect?: () => void }} [options] Optional connection-restored callback
 * @returns {{ ws: Ref<WebSocket | null>, isConnected: Ref<boolean>, isReady: Ref<boolean> }}
 */
export function useSubagentWs(options?: { onReconnect?: () => void }): {
  ws: Ref<WebSocket | null>;
  isConnected: Ref<boolean>;
  isReady: Ref<boolean>;
} {
  const ws: Ref<WebSocket | null> = ref(null);
  const isConnected: Ref<boolean> = ref(false);
  const isReady: Ref<boolean> = ref(false);

  if (subagentWsInstance && subagentWsInstance.readyState === WebSocket.OPEN) {
    ws.value = subagentWsInstance;
    isConnected.value = true;
    isReady.value = subagentReady;
    return { ws, isConnected, isReady };
  }

  const baseUrl = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const wsBase = resolveWsBaseUrl(baseUrl);
  const wsUrl = `${wsBase}/subagents/ws`;

  function connect(): void {
    // Close the old connection
    if (subagentWsInstance) {
      subagentWsInstance.close();
      subagentWsInstance = null;
    }

    const socket = new WebSocket(wsUrl);
    subagentWsInstance = socket;
    subagentReady = false;
    ws.value = socket;

    socket.onopen = () => {
      isConnected.value = true;
      emit('ws:subagents:connected', undefined);
    };

    socket.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        const eventType: string = data.event ?? '';
        const eventData: unknown = data.data ?? null;

        if (eventType === 'ready') {
          subagentReady = true;
          isReady.value = true;
          emit('ws:subagents:ready', eventData);
          return;
        }

        if (eventType === 'subagent_spawned') {
          emit('ws:subagent_spawned', eventData);
        } else if (eventType === 'subagent_ended') {
          emit('ws:subagent_ended', eventData);
        }

        // Pass through the raw event
        emit('ws:subagents:message', data);
      } catch {
        // JSON parse failed; ignore this message
      }
    };

    socket.onclose = () => {
      subagentReady = false;
      isConnected.value = false;
      isReady.value = false;
      ws.value = null;
      subagentWsInstance = null;
      emit('ws:subagents:disconnected', undefined);

      // Auto-reconnect (after 5 seconds)
      setTimeout(() => {
        options?.onReconnect?.();
        connect();
      }, 5000);
    };

    socket.onerror = () => {
      // onclose fires automatically after onerror; reconnection is handled by onclose
    };
  }

  connect();

  return { ws, isConnected, isReady };
}

/**
 * Manually close the subagent WebSocket connection (for cleanup)
 */
export function closeSubagentWs(): void {
  if (subagentWsInstance) {
    subagentWsInstance.close();
    subagentWsInstance = null;
  }
  subagentReady = false;
}
