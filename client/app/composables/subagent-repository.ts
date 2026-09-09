/**
 * Data-access repository for background-task (subagent run) records.
 *
 * Single seam between the subagent state slices and the transport/persistence
 * layers: REST/IPC fetches (bridge), the Dexie cache (db) and the backend
 * SubagentRun ↔ CachedSubagentRun schema mapping all live here, so the slices
 * deal with domain records only.
 */
import type { SubagentRun } from './bridge';
import type { CachedSubagentRun } from './db';

/**
 * Normalize a backend-shaped SubagentRun into the Dexie cache shape CachedSubagentRun.
 * The two shapes share the same field names and differ only in nullability / optional
 * nesting; this does a one-time fallback pass so undefined never gets written into the cache.
 * @param run
 */
export function toCachedSubagentRun(run: SubagentRun): CachedSubagentRun {
  return {
    run_id: run.run_id,
    child_session_key: run.child_session_key ?? null,
    requester_session_key: run.requester_session_key ?? null,
    task: run.task ?? null,
    task_name: run.task_name ?? null,
    label: run.label ?? null,
    spawn_mode: run.spawn_mode ?? null,
    context_mode: run.context_mode ?? null,
    agent_id: run.agent_id ?? null,
    depth: run.depth ?? null,
    role: run.role ?? null,
    control_scope: run.control_scope ?? null,
    generation: run.generation ?? null,
    swarm_group_id: run.swarm_group_id ?? null,
    swarm_run_state: run.swarm_run_state ?? null,
    ended_reason: run.ended_reason ?? null,
    pause_reason: run.pause_reason ?? null,
    execution: run.execution
      ? {
          status: run.execution.status ?? null,
          outcome: run.execution.outcome?.status ?? null,
          started_at: run.execution.started_at != null ? String(run.execution.started_at) : null,
          completed_at: run.execution.ended_at != null ? String(run.execution.ended_at) : null
        }
      : null,
    completion: run.completion
      ? {
          required: run.completion.required ?? null,
          owner_session_key: null,
          result_text: run.completion.result_text ?? null,
          captured_at: run.completion.captured_at != null ? String(run.completion.captured_at) : null
        }
      : null,
    delivery: run.delivery
      ? {
          status: run.delivery.status ?? null,
          attempt_count: run.delivery.attempt_count ?? null,
          delivered_at: run.delivery.delivered_at != null ? String(run.delivery.delivered_at) : null
        }
      : null
  };
}

/**
 * Restore a Dexie cache-shaped record back into the UI display shape SubagentRun.
 * @param c
 */
export function toSubagentRun(c: CachedSubagentRun): SubagentRun {
  return {
    run_id: c.run_id,
    task_run_id: null,
    child_session_key: c.child_session_key ?? '',
    requester_session_key: c.requester_session_key ?? '',
    task: c.task ?? '',
    task_name: c.task_name ?? undefined,
    label: c.label ?? undefined,
    spawn_mode: c.spawn_mode ?? undefined,
    context_mode: c.context_mode ?? undefined,
    agent_id: c.agent_id ?? undefined,
    depth: c.depth ?? undefined,
    role: c.role ?? undefined,
    control_scope: c.control_scope ?? undefined,
    generation: c.generation ?? undefined,
    swarm_group_id: c.swarm_group_id ?? undefined,
    swarm_run_state: c.swarm_run_state ?? undefined,
    ended_reason: c.ended_reason ?? undefined,
    pause_reason: c.pause_reason ?? undefined,
    execution: {
      status: c.execution?.status ?? 'UNKNOWN',
      started_at: c.execution?.started_at != null ? Number(c.execution.started_at) : null,
      ended_at: c.execution?.completed_at != null ? Number(c.execution.completed_at) : null,
      outcome: c.execution?.outcome ? { status: c.execution.outcome, error: null } : { status: 'PENDING', error: null },
      transcript_target: undefined
    },
    completion: {
      required: c.completion?.required ?? false,
      result_text: c.completion?.result_text ?? null,
      captured_at: c.completion?.captured_at != null ? Number(c.completion.captured_at) : null
    },
    delivery: {
      status: c.delivery?.status ?? 'PENDING',
      payload: undefined,
      attempt_count: c.delivery?.attempt_count ?? 0,
      last_error: undefined,
      last_attempt_at: undefined,
      suspended_at: undefined,
      discard_reason: undefined,
      delivered_at: c.delivery?.delivered_at != null ? Number(c.delivery.delivered_at) : undefined
    }
  };
}

/**
 * Map the given backend run records into the cache shape and persist them
 * (deduplicated by `run_id`, overwrites supported too).
 * @param runs Subagent run records to cache
 */
export async function cacheRuns(runs: SubagentRun[]): Promise<void> {
  return cacheSubagentRuns(runs.map(toCachedSubagentRun));
}

/** Read all cached subagent run records. */
export function readCachedRuns(): Promise<CachedSubagentRun[]> {
  return readCachedSubagentRuns();
}

/**
 * Delete the cached run records with the given `run_id`s.
 * @param runIds
 */
export function deleteCachedRuns(runIds: string[]): Promise<void> {
  return deleteCachedSubagentRuns(runIds);
}

/**
 * Fetch the descendant run tree spawned under a session (gap filling after
 * first load / session switch / WS reconnection).
 * @param sessionId
 */
export function fetchSessionRuns(sessionId: string): Promise<SubagentRun[]> {
  return fetchSubagentRuns(sessionId, 'descendants');
}

/**
 * Fetch a single run plus its entire descendant subtree (scoped refresh of the
 * focused task box).
 * @param runId
 */
export function fetchRunSubtree(runId: string): Promise<SubagentRun[]> {
  return fetchSubagentRunSubtree(runId);
}

/**
 * Delete a root run and its entire descendant subtree on the backend.
 * @param runId
 */
export function deleteRunSubtree(runId: string): Promise<number> {
  return deleteSubagentRunSubtree(runId);
}

/**
 * Collect the ids of all sessions that "still exist": the authoritative server
 * `/sessions` list plus the local Dexie session placeholders. Used for the
 * "back to session" existence checks on orphaned runs.
 */
export async function getExistingSessionIds(): Promise<Set<string>> {
  const sessions = await getSessionList();
  const placeholders = await readCachedSessionMetaList();
  const set = new Set<string>();
  for (const s of sessions) if (s.id) set.add(s.id);
  for (const p of placeholders) if (p.id) set.add(p.id);
  return set;
}
