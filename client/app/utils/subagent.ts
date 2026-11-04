import type { SubagentRun } from '~/composables/bridge';

/**
 * Whether the run is still running (RUNNING / INTERRUPTED count as not yet finished)
 * @param run
 */
export function isRunning(run: SubagentRun): boolean {
  const status = statusOf(run?.execution?.status);
  return status === 'RUNNING' || status === 'INTERRUPTED';
}

/** Execution statuses that mean "the run still owns a slot" (running, parked after
 *  an interrupt, or queued behind the sub-agent lane). */
const ACTIVE_EXECUTION_STATUSES = new Set(['RUNNING', 'INTERRUPTED', 'PENDING']);

/** Outcome statuses the registry writes once a run has finished. */
const FINISHED_OUTCOME_STATUSES = new Set(['OK', 'ERROR', 'TIMEOUT', 'KILLED', 'UNKNOWN']);

/**
 * Upper-cased view of a wire status.
 *
 * The run record carries the same vocabulary in either case depending on the
 * transport: the HTTP/WS payloads serialize the backend enums' VALUES
 * (``running`` / ``terminal`` / ``ok``), while the native IPC payload
 * upper-cases them. Comparing on one case keeps both working; the
 * presentation tables (``subagent-status.ts``) are upper-case.
 * @param status
 */
const statusOf = (status: unknown): string => String(status ?? '').toUpperCase();

/**
 * Whether a run is still active work: running, interrupted (recoverable) or
 * queued. Used by the toolbar's terminal entry and its panel.
 *
 * A finished run must never keep sitting in the "running" list, so the status
 * alone is not trusted: an end timestamp, an end reason or a terminal outcome
 * all mean the run is done, whatever the status field still says (a run whose
 * ``subagent_ended`` frame was missed during a reload keeps a stale ``running``
 * status until the next fetch).
 * @param run
 */
export function isActiveRun(run: SubagentRun): boolean {
  const execution = run?.execution;
  if (!ACTIVE_EXECUTION_STATUSES.has(statusOf(execution?.status))) return false;
  if (execution?.ended_at != null) return false;
  if (run?.ended_reason) return false;
  if (FINISHED_OUTCOME_STATUSES.has(statusOf(execution?.outcome?.status))) return false;
  return true;
}
