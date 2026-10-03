import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { sessionIdFromPathname } from '~/utils/session-route';

/**
 * TaskFlow progress store: the waves the floating panel renders.
 *
 * Data flow (same shape as the todo store, one event pair per feature):
 * - The backend pushes `{"event":"taskflow_updated","session_id":…,"content":
 *   {"flows":[…],"totals":{…}}}` after every flow mutation (see
 *   `agent/tools/taskflow/progress_push.py`). The long-lived `/sessions/ws`
 *   singleton forwards those frames as the `ws:taskflow_updated` mitt event, and
 *   the agent stream socket forwards the same frames while a turn is running.
 * - On reconnect (and on first mount) the store asks for a snapshot with a
 *   `ws:send` frame carrying `event: "taskflow_refresh"`; the backend answers with
 *   the identical payload, so there is exactly one shape to render.
 *
 * The frontend never recomputes DAG state: waves, counts and `current_wave` come
 * from the backend (`agent/tools/taskflow/waves.py`).
 */

/** One step row on the wire. */
export interface FlowStep {
  step_id: string;
  task: string;
  status: string;
}

/** One DAG wave: parallel-ready steps at the same dependency depth. */
export interface FlowWave {
  index: number;
  total: number;
  done: number;
  by_status: Record<string, number>;
  steps: FlowStep[];
  /** Present (true) on the trailing wave that holds dependency cycles. */
  cyclic?: boolean;
}

/** One flow's progress. */
export interface FlowProgress {
  flow_id: string;
  status: string;
  description: string;
  total: number;
  done: number;
  /** First wave with unfinished work; 0 = nothing left (or no steps). */
  current_wave: number;
  by_status: Record<string, number>;
  waves: FlowWave[];
}

/** The aggregate the collapsed pill shows. */
export interface ProgressTotals {
  flows: number;
  total: number;
  done: number;
  current_wave: number;
  waves: number;
}

const EMPTY_TOTALS: ProgressTotals = { flows: 0, total: 0, done: 0, current_wave: 0, waves: 0 };

/**
 * Resolve the active session id from the URL path (module-level safe).
 * @returns The trailing path segment, or `''` when it is not a session.
 */
function resolveSid(): string {
  if (typeof window === 'undefined') return '';
  return sessionIdFromPathname(window.location.pathname) ?? '';
}

export const useTaskflowStore = defineStore('taskflow', () => {
  /** Current session's active flows (replaced wholesale on every frame). */
  const flows = ref<FlowProgress[]>([]);
  /** Aggregate over `flows` (sent by the backend; never recomputed here). */
  const totals = ref<ProgressTotals>({ ...EMPTY_TOTALS });
  /** Session id used for refresh; updated by `setCurrentSid` / `init`. */
  const currentSid = ref('');
  /** Singleton guard: listeners are registered exactly once per store instance. */
  const subscribed = ref(false);

  /**
   * Apply a `taskflow_updated` payload (push or refresh reply).
   * @param payload
   */
  function setPayload(payload: unknown): void {
    const content = (payload as { content?: { flows?: unknown; totals?: unknown } } | null)?.content;
    flows.value = Array.isArray(content?.flows) ? (content.flows as FlowProgress[]) : [];
    totals.value = { ...EMPTY_TOTALS, ...((content?.totals as ProgressTotals) ?? {}) };
  }

  /** Register the mitt listeners once (singleton guard). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:taskflow_updated', setPayload);
    // The backend pushes nothing spontaneously after a reconnect: ask for a snapshot.
    on('ws:reconnected', () => {
      refresh();
    });
  }

  /**
   * Ask the backend for the current flows (outbound `ws:send` frame).
   * @param sid Optional explicit session id (defaults to the last known/URL sid).
   */
  function refresh(sid?: string): void {
    const target = sid ?? currentSid.value ?? resolveSid();
    currentSid.value = target ?? '';
    if (!currentSid.value) return;
    emit('ws:send', { event: 'taskflow_refresh', session_id: currentSid.value, content: '' });
  }

  /**
   * Set the session id used for refresh.
   * @param sid
   */
  function setCurrentSid(sid: string): void {
    currentSid.value = sid;
  }

  /**
   * Initialise for a session: install the listeners once and pull a snapshot.
   * Idempotent; safe to call from every consumer mount.
   * @param sid Current session id (route param).
   */
  function init(sid?: string): void {
    subscribe();
    if (sid) currentSid.value = sid;
    if (currentSid.value) refresh(currentSid.value);
  }

  /** Drop the session's progress (session switch, cleared board). */
  function clear(): void {
    flows.value = [];
    totals.value = { ...EMPTY_TOTALS };
  }

  /** Whether there is anything to show (no flows, no panel). */
  const hasProgress = computed(() => flows.value.length > 0);

  /** Percentage of finished steps across the flows (0 when nothing is tracked). */
  const percent = computed(() =>
    totals.value.total > 0 ? Math.round((totals.value.done / totals.value.total) * 100) : 0
  );

  return {
    flows,
    totals,
    currentSid,
    subscribed,
    hasProgress,
    percent,
    clear,
    init,
    refresh,
    setCurrentSid,
    setPayload,
    subscribe
  };
});
