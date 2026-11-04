/**
 * Live trajectory stream for one session (WS, with reconnect).
 *
 * The backend channel (`server/trigger/ws/trajectory_ws.py`) sends a `ready`
 * frame carrying the newest event page, then one `trajectory_event` frame per
 * persisted row. Rows are merged by `event_id` exactly like the ledger's
 * upsert: `tool_start` and `tool_end` share that id, so the end's
 * result/duration is folded into the start's args (the DB keeps the first
 * seq/ts, and so does the merge).
 *
 * A dropped socket reconnects on its own; the ready snapshot then gap-fills
 * whatever was missed while the channel was down.
 */

import { onBeforeUnmount, ref, watch } from 'vue';
import type { Ref } from 'vue';
import type { TrajectoryEvent } from '~/composables/bridge/session';

/** Delay before re-opening a dropped socket. */
const RECONNECT_DELAY_MS = 2000;

/**
 * Merge incoming events into the current list (event-id identity + payload fold).
 * @param current The rendered list.
 * @param incoming Frame events (new rows, or the ready snapshot).
 * @returns The sorted merged list.
 */
function mergeEvents(current: TrajectoryEvent[], incoming: TrajectoryEvent[]): TrajectoryEvent[] {
  if (!incoming.length) return current;
  const byId = new Map<string, TrajectoryEvent>(current.map(event => [event.event_id, event]));
  for (const event of incoming) {
    const existing = byId.get(event.event_id);
    if (!existing) {
      byId.set(event.event_id, event);
      continue;
    }
    // Same upsert key (a tool call's start + end): the payloads fold, while the
    // ledger keeps the FIRST row's seq/ts (its ON CONFLICT touches only
    // is_error / summary / payload).
    byId.set(event.event_id, {
      ...existing,
      ...event,
      seq: existing.seq,
      ts: existing.ts,
      payload: { ...existing.payload, ...event.payload }
    });
  }
  return [...byId.values()].sort(compareEvents);
}

/**
 * Subscribe to one session's live trajectory.
 * @param sessionId The session under watch (null = no socket).
 */
export function useTrajectoryStream(sessionId: Ref<string | null>) {
  const events = ref<TrajectoryEvent[]>([]);
  const connected = ref(false);

  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;

  /** Stop the socket and any pending reconnect. */
  const teardown = (): void => {
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = undefined;
    const current = socket;
    socket = null;
    if (!current) return;
    current.onopen = null;
    current.onmessage = null;
    current.onerror = null;
    current.onclose = null;
    try {
      current.close();
    } catch {
      // A socket that is already closed is not an error.
    }
  };

  /**
   * Enqueue a reconnect after a dropped socket (never after a dispose).
   */
  const scheduleReconnect = (): void => {
    if (disposed || retryTimer || !sessionId.value) return;
    retryTimer = setTimeout(() => {
      retryTimer = undefined;
      if (!disposed) connect();
    }, RECONNECT_DELAY_MS);
  };

  /** Open the socket for the current session (no-op without one). */
  function connect(): void {
    const sid = sessionId.value;
    if (disposed || !sid || typeof WebSocket === 'undefined') return;
    teardown();
    // The handshake carries the per-boot gateway token and (when login is on)
    // a single-use ticket — both must be refreshed for every attempt.
    void ensureGatewayToken();
    void prepareWsTicket();
    const url = appendWsTicket(
      withGatewayToken(`${WS_BASE_URL}/sessions/trajectory/ws?session_id=${encodeURIComponent(sid)}`)
    );
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    socket = ws;

    ws.onopen = () => {
      if (socket !== ws) return;
      connected.value = true;
    };
    ws.onmessage = message => {
      if (socket !== ws) return;
      let frame: { event?: string; data?: unknown; events?: unknown };
      try {
        frame = JSON.parse(String(message.data)) as typeof frame;
      } catch {
        return;
      }
      if (frame.event === 'ready') {
        const snapshot = Array.isArray(frame.events) ? (frame.events as TrajectoryEvent[]) : [];
        events.value = mergeEvents(events.value, snapshot);
        return;
      }
      if (frame.event === 'trajectory_event' && frame.data && typeof frame.data === 'object') {
        events.value = mergeEvents(events.value, [frame.data as TrajectoryEvent]);
      }
    };
    ws.onerror = () => {
      // `onclose` follows and owns the reconnect.
      try {
        ws.close();
      } catch {
        // Already closing.
      }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      connected.value = false;
      scheduleReconnect();
    };
  }

  watch(
    sessionId,
    () => {
      events.value = [];
      connected.value = false;
      teardown();
      connect();
    },
    { immediate: true }
  );

  onBeforeUnmount(() => {
    disposed = true;
    connected.value = false;
    teardown();
  });

  return { events, connected };
}
