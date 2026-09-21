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
 * Both channels share the connection lifecycle (connect / 5s fixed-delay
 * reconnect / superseded-socket guard / cleanup) through `ws-connection.ts`;
 * this module owns the module-level singletons and the channel-specific state
 * (session heartbeat counters live in the base, the subagent `ready` handshake
 * lives here).
 *
 * @module ws
 */

import { ref, type Ref } from 'vue';

/** Session ID (currently fixed to "default") */
const SESSION_ID = 'default';

/** Heartbeat send interval (milliseconds) */
const HEARTBEAT_INTERVAL_MS = 10000;

/** Pong timeout window: if no server frame arrives within this duration after a ping is sent, count one timeout (milliseconds) */
const PONG_TIMEOUT_MS = 5000;

/** Consecutive pong timeout threshold: the connection is declared dead only when this count is reached */
const MAX_MISSED_PONGS = 2;

/** Fixed auto-reconnect delay shared by both push channels (milliseconds) */
const RECONNECT_DELAY_MS = 5000;

/** Session-push channel singleton (replaced by every explicit connect) */
let sessionChannel: WsConnection | null = null;

/** Whether the session channel ever reached OPEN (drives the `ws:reconnected` emit) */
let everConnected = false;

/** Outbound bridge: sends a full `{session_id, event, content}` frame (same shape as the heartbeat send). */
on('ws:send', (payload: unknown) => {
  const frame = payload as { event?: unknown } | null | undefined;
  if (!frame || typeof frame.event !== 'string') return;
  const socket = sessionChannel?.socket;
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify(frame));
});

/**
 * Create and obtain the session-push WebSocket connection (singleton)
 *
 * Equivalent of @st.cache_resource: module-level singleton + connection state
 * management. The 10s ping / 5s pong-timeout heartbeat runs on this channel
 * only; a dead link is closed and the existing 5s auto-reconnect rebuilds it.
 *
 * @param {{ onReconnect?: () => void }} [options] Optional connection-restored callback
 * @param options.onReconnect
 * @returns {{ ws: Ref<WebSocket | null>, isConnected: Ref<boolean> }}
 */
export function useWs(options?: { onReconnect?: () => void }): {
  ws: Ref<WebSocket | null>;
  isConnected: Ref<boolean>;
} {
  const ws: Ref<WebSocket | null> = ref(null);
  const isConnected: Ref<boolean> = ref(false);

  // A healthy singleton is shared as-is (the caller binds its own refs to it).
  if (sessionChannel?.isOpen) {
    ws.value = sessionChannel.socket;
    isConnected.value = true;
    return { ws, isConnected };
  }

  // An existing connection is still handshaking: reuse it directly, never
  // close and rebuild — otherwise multiple callers (connection.ts startup +
  // NotificationDialog mount) would close each other's not-yet-finished
  // connections, and both sides' onclose would schedule 5s reconnects,
  // creating a "reconnect storm".
  if (sessionChannel?.isConnecting) {
    ws.value = sessionChannel.socket;
    return { ws, isConnected };
  }

  // Replace the previous channel: disposing it cancels its pending
  // auto-reconnect, so a stale timer can never rebuild a socket after this
  // explicit connect.
  sessionChannel?.dispose();

  const handleSessionFrame = createWsMessageHandler<{ content?: unknown }>({
    notification: data => emit('ws:notification', data.content ?? ''),
    todo_updated: data => emit('ws:todo_updated', data)
  });

  const channel = new WsConnection({
    url: `${WS_BASE_URL}/sessions/ws?session_id=${SESSION_ID}`,
    reconnectDelayMs: RECONNECT_DELAY_MS,
    heartbeat: {
      intervalMs: HEARTBEAT_INTERVAL_MS,
      timeoutMs: PONG_TIMEOUT_MS,
      maxMissed: MAX_MISSED_PONGS,
      frame: () => ({ session_id: SESSION_ID, event: 'ping', content: '' }),
      onTimeout: () => emit('ws:heartbeat_timeout', undefined)
    },
    onOpen: () => {
      isConnected.value = true;
      emit('ws:connected', undefined);
      if (everConnected) emit('ws:reconnected', undefined);
      everConnected = true;
    },
    onFrame: event => {
      try {
        const data = handleSessionFrame(event);
        if (data) {
          // Pass through the raw event
          emit('ws:message', data);
        }
      } catch {
        // JSON parse failed; ignore this message
      }
    },
    onClose: () => {
      isConnected.value = false;
      ws.value = null;
      emit('ws:disconnected', undefined);
    },
    onReconnect: () => options?.onReconnect?.()
  });

  sessionChannel = channel;
  channel.connect();
  ws.value = channel.socket;

  return { ws, isConnected };
}

/**
 * Manually close the session-push WebSocket connection (for cleanup)
 */
export function closeWs(): void {
  const channel = sessionChannel;
  sessionChannel = null;
  channel?.dispose();
  everConnected = false;
}

/**
 * Whether the session push WebSocket (/sessions/ws) singleton is currently OPEN.
 *
 * Lets external modules such as connection.ts read the singleton's real-time
 * connection state (instead of each maintaining its own mirrored copy).
 */
export function isSessionWsOpen(): boolean {
  return sessionChannel?.isOpen === true;
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
let subagentChannel: WsConnection | null = null;

/** Whether the subagent connection is ready (ready frame received) */
let subagentReady = false;

/**
 * Create and obtain the subagent real-time push WebSocket connection (singleton)
 *
 * @param {{ onReconnect?: () => void }} [options] Optional connection-restored callback
 * @param options.onReconnect
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

  // A healthy singleton is shared as-is (the caller binds its own refs to it).
  if (subagentChannel?.isOpen) {
    ws.value = subagentChannel.socket;
    isConnected.value = true;
    isReady.value = subagentReady;
    return { ws, isConnected, isReady };
  }

  // Replace the previous channel (cancels its pending auto-reconnect).
  subagentChannel?.dispose();

  const channel = new WsConnection({
    url: `${WS_BASE_URL}/subagents/ws`,
    reconnectDelayMs: RECONNECT_DELAY_MS,
    onOpen: () => {
      isConnected.value = true;
      emit('ws:subagents:connected', undefined);
    },
    onFrame: event => {
      try {
        const data = handleSubagentFrame(event);
        // `ready` frames stay private to this module; every other parsed frame is passed through raw
        if (data === null || data.event === 'ready') return;
        emit('ws:subagents:message', data);
      } catch {
        // JSON parse failed; ignore this message
      }
    },
    onClose: () => {
      subagentReady = false;
      isConnected.value = false;
      isReady.value = false;
      ws.value = null;
      emit('ws:subagents:disconnected', undefined);
    },
    onReconnect: () => options?.onReconnect?.()
  });

  const handleSubagentFrame = createWsMessageHandler<{ event?: string; data?: unknown }>({
    ready: data => {
      subagentReady = true;
      isReady.value = true;
      emit('ws:subagents:ready', data.data ?? null);
    },
    subagent_spawned: data => emit('ws:subagent_spawned', data.data ?? null),
    subagent_ended: data => emit('ws:subagent_ended', data.data ?? null)
  });

  subagentChannel = channel;
  subagentReady = false;
  channel.connect();
  ws.value = channel.socket;

  return { ws, isConnected, isReady };
}

/**
 * Manually close the subagent WebSocket connection (for cleanup)
 */
export function closeSubagentWs(): void {
  const channel = subagentChannel;
  subagentChannel = null;
  subagentReady = false;
  channel?.dispose();
}
