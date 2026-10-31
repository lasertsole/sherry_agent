import { computed, ref } from 'vue';

import type { AgentSocket } from './bridge/agent-socket-types';
import type { TerminalBgTaskInfo } from './bridge/agent-socket-types';

/**
 * One session's background commands (the agent's
 * `terminal(run_in_background=True)`), as the toolbar's background-task popup
 * shows them.
 *
 * Two sources, on purpose: `GET /terminal/bg` is the snapshot (the popover
 * opens, the page mounts, a socket reconnects) and the WS frames are the deltas
 * — `terminal_bg_started` adds a row, `terminal_bg_exit` settles it. Polling is
 * absent: a command that starts while the popover is open arrives as a frame.
 *
 * These commands are CHILDREN of the backend process and are not persisted, so
 * the list is rebuilt on every page load — an empty snapshot right after a
 * restart is correct, not a lost task.
 */

/** One row of the popup's background-command section. */
export interface BackgroundTaskItem {
  taskId: string;
  command: string;
  /** running | completed | failed | killed */
  status: string;
  pid: number | null;
  exitCode: number | null;
  /** `killed` when stopped on request, `exited` when it ended by itself. */
  reason: string | null;
}

/** The message the kill ack surfaces when the backend refused it. */
export type KillRefusal = { taskId: string; reason: string };

// `fetchApi` comes from Nuxt's composable auto-import (a relative sibling
// import of a composable is lint-restricted).

/**
 * The background-command list for one session, wired to that session's socket.
 * @param sessionId The session the strip belongs to ('' while unknown).
 * @param socket The session's agent socket (frames + the kill frame).
 */
export function useBackgroundTasks(sessionId: () => string, socket: AgentSocket) {
  const tasks = ref<BackgroundTaskItem[]>([]);
  /** Set when a kill was refused; the panel shows it once and clears it. */
  const killRefusal = ref<KillRefusal | null>(null);
  /** Task ids whose kill was acknowledged but whose exit has not arrived. */
  const killing = ref<Set<string>>(new Set());

  /** Running commands only (what the kill button applies to). */
  const runningCount = computed(() => tasks.value.filter(task => task.status === 'running').length);

  /**
   * Replace the list from the backend snapshot.
   * @param items Rows as `GET /terminal/bg` reports them.
   */
  function applySnapshot(items: TerminalBgTaskInfo[]): void {
    tasks.value = items.map(item => ({
      taskId: item.task_id,
      command: item.command,
      status: item.status,
      pid: item.pid ?? null,
      exitCode: item.exit_code ?? null,
      reason: item.status === 'killed' ? 'killed' : null
    }));
    // A settled command can never still be "stopping": the snapshot is the
    // authority, so its id leaves the in-flight set and the row shows the real
    // terminal status instead of the requested stop.
    if (killing.value.size > 0) {
      const running = new Set(tasks.value.filter(task => task.status === 'running').map(task => task.taskId));
      const next = new Set([...killing.value].filter(taskId => running.has(taskId)));
      if (next.size !== killing.value.size) killing.value = next;
    }
  }

  /** Pull the snapshot (panel open / page mount / socket reconnect). Fail-open. */
  async function load(): Promise<void> {
    const id = sessionId();
    if (!id) return;
    try {
      const response = await fetchApi({
        // `_` is a cache-buster: a snapshot is state, never a cacheable
        // document — a re-pull right after a kill must not answer with the
        // pre-kill body the browser still holds.
        url: `/terminal/bg?session_id=${encodeURIComponent(id)}&_=${Date.now()}`,
        method: 'get'
      });
      const payload = (response ?? {}) as { tasks?: TerminalBgTaskInfo[] };
      applySnapshot(Array.isArray(payload.tasks) ? payload.tasks : []);
    } catch {
      // A failed snapshot leaves whatever the frames already told us.
    }
  }

  /**
   * Ask the backend to stop one command (does not touch the turn).
   * @param taskId The command's task id.
   */
  function kill(taskId: string): void {
    if (!taskId) return;
    killing.value = new Set([...killing.value, taskId]);
    socket.sendTerminalBgKill(taskId);
  }

  /**
   * `terminal_bg_started`: add the row (a repeat start updates it instead).
   * @param info
   */
  function onStarted(info: TerminalBgTaskInfo): void {
    const row: BackgroundTaskItem = {
      taskId: info.task_id,
      command: info.command,
      status: info.status,
      pid: info.pid ?? null,
      exitCode: info.exit_code ?? null,
      reason: null
    };
    const index = tasks.value.findIndex(task => task.taskId === row.taskId);
    if (index === -1) {
      tasks.value = [row, ...tasks.value];
    } else {
      const next = [...tasks.value];
      next[index] = row;
      tasks.value = next;
    }
  }

  /**
   * `terminal_bg_exit`: the command's terminal state (the panel's authority).
   * @param info
   * @param info.taskId
   * @param info.status
   * @param info.reason
   * @param info.exitCode
   */
  function onExit(info: { taskId: string; status: string; reason: string; exitCode: number | null }): void {
    const index = tasks.value.findIndex(task => task.taskId === info.taskId);
    const existing = index === -1 ? null : tasks.value[index];
    const row: BackgroundTaskItem = {
      taskId: info.taskId,
      command: existing?.command ?? '',
      status: info.status,
      pid: existing?.pid ?? null,
      exitCode: info.exitCode,
      reason: info.reason
    };
    if (index === -1) {
      tasks.value = [...tasks.value, row];
    } else {
      const next = [...tasks.value];
      next[index] = row;
      tasks.value = next;
    }
    if (killing.value.has(info.taskId)) {
      const nextKilling = new Set(killing.value);
      nextKilling.delete(info.taskId);
      killing.value = nextKilling;
    }
  }

  /**
   * `terminal_bg_kill_ack`: the signal landed, or the backend refused it.
   * @param info
   * @param info.taskId
   * @param info.ok
   * @param info.reason
   */
  function onKillAck(info: { taskId: string; ok: boolean; reason?: string | null }): void {
    if (info.ok) return;
    const nextKilling = new Set(killing.value);
    nextKilling.delete(info.taskId);
    killing.value = nextKilling;
    killRefusal.value = {
      taskId: info.taskId,
      reason: info.reason || 'not_running'
    };
  }

  return {
    tasks,
    runningCount,
    killing,
    killRefusal,
    load,
    kill,
    onStarted,
    onExit,
    onKillAck
  };
}
