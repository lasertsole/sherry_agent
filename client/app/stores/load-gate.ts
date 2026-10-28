/**
 * The SUBAGENT lane's load-gate notice.
 *
 * When the host is at the hard-stop threshold the load gate defers a subagent
 * dispatch instead of queueing it in the lane; the server pushes one
 * `load_warning` frame per run (best effort). This store turns that frame into
 * a Notifications entry — the same channel as the search-backend downgrade —
 * because the event is process-level, arrives outside any component, and a
 * reload drops it (the server keeps retrying meanwhile).
 *
 * Nothing renders while the gate is disabled (the shipped default), and the
 * web build without a socket simply never receives the frame.
 *
 * @module stores/load-gate
 */
import { defineStore } from 'pinia';
import { ref } from 'vue';

// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { on } from '@/composables/mitt';

/** The last load verdict the server reported, for display. */
export interface LoadWarning {
  /** CPU load percentage at the moment of the sample. */
  cpu: number;
  /** Memory load percentage at the moment of the sample. */
  ram: number;
  /** The deferred run's id (one notice per run). */
  runId: string;
}

/**
 * The localized one-line notice. Built here because the frame arrives on a
 * non-component path (a store subscriptions), where `useI18n` may be absent:
 * the runtime resolver falls back to the frame's own numbers.
 * @param cpu
 * @param ram
 * @returns The notice to show in Notifications.
 */
export function loadWarningText(cpu: number, ram: number): string {
  const t = resolveRuntimeT();
  const key = 'loadGate.warning';
  if (!t) return `主机负载过高，子代理派发已暂缓（CPU ${cpu}% / 内存 ${ram}%）`;
  const text = t(key, { cpu, ram });
  return text === key ? `CPU ${cpu}% / RAM ${ram}%` : text;
}

export const useLoadGateStore = defineStore('loadGate', () => {
  /** The most recent deferred dispatch, or null. */
  const lastWarning = ref<LoadWarning | null>(null);
  /** Singleton guard: the socket listener is registered exactly once. */
  const subscribed = ref(false);

  /**
   * Turn one `load_warning` frame into a Notifications entry.
   * @param payload Raw frame payload (`{session_id, content: {cpu, ram, run_id}}`).
   */
  function handleLoadWarning(payload: unknown): void {
    const frame = payload as
      { session_id?: string; content?: { cpu?: unknown; ram?: unknown; run_id?: unknown } } | null | undefined;
    if (!String(frame?.session_id ?? '')) return;
    const cpu = Number(frame?.content?.cpu ?? 0);
    const ram = Number(frame?.content?.ram ?? 0);
    const runId = String(frame?.content?.run_id ?? '');
    lastWarning.value = { cpu: Number.isFinite(cpu) ? cpu : 0, ram: Number.isFinite(ram) ? ram : 0, runId };
    useNotificationStore().handleNotification(
      loadWarningText(lastWarning.value.cpu, lastWarning.value.ram),
      'load_gate'
    );
  }

  /** Register the bus listener once (idempotent, like the other stores). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:load_warning', handleLoadWarning);
  }

  return { lastWarning, subscribed, subscribe, handleLoadWarning };
});
