/**
 * Session-scoped bridge calls: history, session clear and subagent run
 * management (background tasks).
 *
 * Native (Tauri) mode mirrors the REST endpoints through IPC commands; browser
 * mode hits the Python REST API directly.
 *
 * @module bridge/session
 */
import type { HistoryMessage } from '~/types/backend/HistoryMessage';
import type { Response as ApiResponse } from '~/types/response';
import { invokeNative } from './transport';

/**
 * A single sub-agent run record surfaced to the "background tasks" tab.
 *
 * Mirrors the serialized `SubagentRunRecord` returned by `GET /subagents/runs`
 * on the backend. Only the public/safe subset of fields is exposed.
 */
export interface SubagentRun {
  run_id: string;
  task_run_id?: string | null;
  child_session_key: string;
  requester_session_key: string;
  task: string;
  task_name?: string | null;
  label?: string | null;
  spawn_mode?: string;
  context_mode?: string;
  agent_id?: string;
  depth?: number;
  role?: string;
  control_scope?: string;
  generation?: number;
  swarm_group_id?: string | null;
  swarm_run_state?: string | null;
  ended_reason?: string | null;
  pause_reason?: string | null;
  execution: {
    status: string;
    started_at: number | null;
    ended_at: number | null;
    outcome: { status: string; error: string | null } | null;
    transcript_target?: string | null;
  };
  completion: {
    required: boolean;
    result_text: string | null;
    captured_at: number | null;
  };
  delivery: {
    status: string;
    payload?: string | null;
    attempt_count?: number;
    last_error?: string | null;
    last_attempt_at?: number | null;
    suspended_at?: number | null;
    discard_reason?: string | null;
    delivered_at?: number | null;
  };
}

/**
 * Clear all state for a session.
 * @param sessionId
 */
export async function clearSession(sessionId: string): Promise<void> {
  const native = await invokeNative('session_clear', { request: { session_id: sessionId } });
  if (native === null) {
    await fetchApi({
      url: '/sessions',
      opts: { session_id: sessionId },
      method: 'delete'
    });
  }
}

/**
 * Fetch the list of sub-agent runs spawned under a session (background tasks).
 *
 * In Tauri mode this calls the matching IPC command; in browser mode it hits
 * the Python `GET /subagents/runs` endpoint directly.
 *
 * @param sessionId Session whose descendant sub-agent runs should be returned.
 * @param scope "descendants" (default) returns the full spawned tree;
 *              "controller" returns runs where the session is requester or child.
 */
export async function fetchSubagentRuns(
  sessionId: string,
  scope: 'descendants' | 'controller' = 'descendants'
): Promise<SubagentRun[]> {
  const native = await invokeNative<{ runs: SubagentRun[] }>('subagent_runs', {
    request: { session_id: sessionId, scope }
  });
  if (native !== null) return native.value.runs ?? [];
  const res: ApiResponse = await fetchApi({
    url: '/subagents/runs',
    opts: { session_id: sessionId, scope },
    method: 'get'
  });
  const resp = (res as unknown as { runs?: SubagentRun[] }).runs ?? [];
  return Array.isArray(resp) ? resp : [];
}

/**
 * Fetch a single sub-agent run plus its entire descendant subtree (background tasks).
 *
 * Used to perform a scoped refresh of only the currently focused task tree box,
 * instead of re-fetching the whole session.
 *
 * In Tauri mode this calls the matching IPC command; in browser mode it hits
 * the Python `GET /subagents/runs?run_id=...` endpoint directly.
 *
 * @param runId The root run whose subtree (including itself) should be returned.
 */
export async function fetchSubagentRunSubtree(runId: string): Promise<SubagentRun[]> {
  const native = await invokeNative<{ runs: SubagentRun[] }>('subagent_runs', {
    request: { run_id: runId }
  });
  if (native !== null) return native.value.runs ?? [];
  const res: ApiResponse = await fetchApi({
    url: '/subagents/runs',
    opts: { run_id: runId },
    method: 'get'
  });
  const resp = (res as unknown as { runs?: SubagentRun[] }).runs ?? [];
  return Array.isArray(resp) ? resp : [];
}

/**
 * Delete a sub-agent run and its entire descendant subtree (background tasks).
 *
 * Permanently removes the root run plus all of its descendants from the
 * backend registry (in-memory + SQLite) and clears its attachments dir.
 *
 * In Tauri mode this calls the matching IPC command; in browser mode it hits
 * the Python `DELETE /subagents/runs` endpoint directly.
 *
 * @param runId The root run id whose subtree should be removed.
 * @returns The number of runs removed from the backend.
 */
export async function deleteSubagentRunSubtree(runId: string): Promise<number> {
  const native = await invokeNative<{ success: boolean; removed: number }>('subagent_run_delete', {
    request: { run_id: runId }
  });
  if (native !== null) return native.value?.removed ?? 0;
  const res: ApiResponse = await fetchApi({
    url: '/subagents/runs',
    opts: { run_id: runId },
    method: 'delete'
  });
  const resp = (res as unknown as { success?: boolean; removed?: number }) ?? {};
  return typeof resp.removed === 'number' ? resp.removed : 0;
}

/**
 * Steer (redirect / resume) a running sub-agent run (background tasks).
 *
 * Cancels the run's current execution and re-dispatches the child agent on the
 * SAME checkpointer thread with the new direction injected (generation +1), so
 * the child continues from its persisted conversation state. Works for
 * RUNNING/INTERRUPTED runs; an empty payload simply resumes an interrupted
 * run. Also revives a run orphaned by a backend restart (zombie RUNNING).
 *
 * Browser/Tauri webview mode hits the Python `POST /subagents/steer` endpoint
 * directly (backend sends `Access-Control-Allow-Origin: *`, so no dedicated
 * Tauri IPC command is required).
 *
 * @param runId The run to steer.
 * @param payload New task and/or additional instructions; both optional.
 * @param payload.new_task New task direction injected into the child agent.
 * @param payload.new_instructions Additional steering instructions appended to the thread.
 * @returns The updated run record, or null when the backend rejected the steer
 *          (terminal/collector state, rate-limited, control denied) or the
 *          request failed.
 */
export async function steerSubagentRun(
  runId: string,
  payload: { new_task?: string; new_instructions?: string } = {}
): Promise<SubagentRun | null> {
  const res: ApiResponse = await fetchApi({
    url: '/subagents/steer',
    opts: { run_id: runId, ...payload },
    method: 'post'
  });
  const resp = (res as unknown as { run?: SubagentRun }) ?? {};
  return resp.run ?? null;
}

/**
 * Retrieve conversation history.
 * @param sessionId
 * @param lastTurnCount
 */
export async function getHistory(sessionId: string, lastTurnCount: number = 10): Promise<HistoryMessage[]> {
  const native = await invokeNative<HistoryMessage[]>('session_history', {
    request: { session_id: sessionId, last_turn_count: lastTurnCount }
  });
  if (native !== null) return native.value;
  return fetchApi({
    url: '/n_turns_history_messages',
    opts: { session_id: sessionId, last_turn_count: lastTurnCount },
    method: 'get'
  }) as unknown as Promise<HistoryMessage[]>;
}
