/**
 * One sub-agent run's activity state: the run record, its child session (the
 * trajectory source) and the steer action.
 *
 * The run lives in the shared `stores/subagent.ts` lists, which the
 * `/subagents/ws` sync keeps fresh; this composable only derives what the
 * activity tab needs and writes a steered run back into both lists so the
 * header/summary update immediately.
 */

import { computed } from 'vue';
import type { Ref } from 'vue';
import type { SubagentRun } from '~/composables/bridge';
// Nested bridge modules are not part of the Nuxt auto-import set (same explicit
// import convention as the session page / TrajectoryPanel).
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { steerSubagentRun } from '~/composables/bridge/session';
import { useSubagentStore } from '~/stores/subagent';
import { isRunning } from '~/utils/subagent';

/**
 * Activity state for one run.
 * @param runId The run under watch (null = no tab).
 */
export function useSubagentActivity(runId: Ref<string | null>) {
  const store = useSubagentStore();

  /** The run record, from either list (the global one covers ended runs). */
  const run = computed<SubagentRun | null>(() => {
    const id = runId.value;
    if (!id) return null;
    return (
      (store.allTaskRuns ?? []).find(entry => entry.run_id === id) ??
      (store.taskRuns ?? []).find(entry => entry.run_id === id) ??
      null
    );
  });

  /** The child session whose trajectory ledger is the live view's source. */
  const childSessionKey = computed<string | null>(() => {
    const key = run.value?.child_session_key;
    return typeof key === 'string' && key ? key : null;
  });

  /** Steering is only meaningful while the run still owns a slot. */
  const isSteerable = computed<boolean>(() => (run.value ? isRunning(run.value) : false));

  /**
   * Write a steered run back into both lists (the WS also refreshes them, but
   * the header must not wait for the next frame).
   * @param updated Fresh run record from the backend.
   */
  const mergeRun = (updated: SubagentRun): void => {
    const merge = (list: SubagentRun[]): SubagentRun[] =>
      list.map(entry => (entry.run_id === updated.run_id ? { ...entry, ...updated } : entry));
    store.allTaskRuns = merge(store.allTaskRuns ?? []);
    store.taskRuns = merge(store.taskRuns ?? []);
  };

  /**
   * Send new instructions to the run.
   * @param instructions Free-text guidance for the child agent.
   * @returns True when the backend accepted it.
   */
  async function steer(instructions: string): Promise<boolean> {
    const current = run.value;
    const text = instructions.trim();
    if (!current || !text || !isSteerable.value) return false;
    const updated = await steerSubagentRun(current.run_id, { new_instructions: text });
    if (updated) mergeRun(updated);
    return Boolean(updated);
  }

  return { run, childSessionKey, isSteerable, steer };
}
