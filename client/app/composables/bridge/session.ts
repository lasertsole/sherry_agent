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
  const res = await fetchApiPayload<{ runs?: SubagentRun[] }>({
    url: '/subagents/runs',
    opts: { session_id: sessionId, scope },
    method: 'get'
  });
  const resp = res.runs ?? [];
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
  const res = await fetchApiPayload<{ runs?: SubagentRun[] }>({
    url: '/subagents/runs',
    opts: { run_id: runId },
    method: 'get'
  });
  const resp = res.runs ?? [];
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
  const res = await fetchApi<{ success?: boolean; removed?: number }>({
    url: '/subagents/runs',
    opts: { run_id: runId },
    method: 'delete'
  });
  const resp = res ?? {};
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
  const res = await fetchApi<{ run?: SubagentRun }>({
    url: '/subagents/steer',
    opts: { run_id: runId, ...payload },
    method: 'post'
  });
  const resp = res ?? {};
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
  return fetchApiPayload<HistoryMessage[]>({
    url: '/n_turns_history_messages',
    opts: { session_id: sessionId, last_turn_count: lastTurnCount },
    method: 'get'
  });
}

/**
 * Thinking control payload shapes. `mode: "on_off"` models use the boolean
 * switch; `mode: "levels"` models are always-think gateways whose control is
 * a 低/高/最高 selector (`level: "low" | "high" | "max"`).
 */
export type ThinkingMode = 'on_off' | 'levels';
export type ThinkingValue = boolean | 'low' | 'high' | 'max';

/** Toolbar shield control: how much the session may do unattended. */
export type AccessMode = 'confirm_all' | 'auto_edit' | 'full_access';

/** Modes the backend understands, for narrowing a raw payload value. */
const ACCESS_MODES: AccessMode[] = ['confirm_all', 'auto_edit', 'full_access'];

/**
 * Narrow a raw ``mode`` payload value to an :type:`AccessMode`.
 * @param raw
 */
const toAccessMode = (raw: string | undefined): AccessMode => ACCESS_MODES.find(mode => mode === raw) ?? 'auto_edit';

/**
 * Read the session's access mode (``auto_edit`` unless another one was chosen).
 * @param sessionId
 */
export async function fetchAccessMode(sessionId: string): Promise<AccessMode> {
  const res = await fetchApiPayload<{ mode?: string }>({
    url: '/sessions/access_mode',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return toAccessMode(res.mode);
}

/**
 * Switch the session's access mode; applies from the next tool call on.
 * ``confirm_all`` and ``full_access`` are mutually exclusive — the backend
 * clears the other flag on every write, and ``auto_edit`` clears both.
 * @param sessionId
 * @param mode
 */
export async function setAccessMode(sessionId: string, mode: AccessMode): Promise<AccessMode> {
  const res = await fetchApiPayload<{ mode?: string }>({
    url: '/sessions/access_mode',
    opts: { session_id: sessionId, mode },
    method: 'put'
  });
  return toAccessMode(res.mode);
}

export interface ContextUsage {
  /** Context window of the configured main LLM. */
  window: number;
  /** Prompt size the provider reported for the session's last finished turn. */
  total: number;
  /** System-prompt estimate, skill index excluded. */
  system: number;
  /** Skill-index estimate (the ``<available_skills>`` block). */
  skills: number;
  /** Main tool-schema estimate. */
  tools: number;
  /** Rest of the reported prompt (the conversation itself). */
  messages: number;
  /** Session-wide cached-prompt share (cached / prompt tokens), null when unknown. */
  cache_hit_ratio: number | null;
  /** Pressure at which summarization compacts the session (share of the window). */
  compress_ratio: number;
}

/**
 * Read the session's context accounting: the window, the reported prompt size
 * and how it splits into system prompt / tool schemas / messages.
 * @param sessionId
 */
export async function fetchContextUsage(sessionId: string): Promise<ContextUsage> {
  const res = await fetchApiPayload<ContextUsage>({
    url: '/context_usage',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return {
    window: Number(res.window ?? 0),
    total: Number(res.total ?? 0),
    system: Number(res.system ?? 0),
    skills: Number(res.skills ?? 0),
    tools: Number(res.tools ?? 0),
    messages: Number(res.messages ?? 0),
    cache_hit_ratio: typeof res.cache_hit_ratio === 'number' ? res.cache_hit_ratio : null,
    compress_ratio: Number(res.compress_ratio ?? 0)
  };
}

export interface ThinkingState {
  mode: ThinkingMode;
  enabled: boolean | null;
  level: 'low' | 'high' | 'max' | null;
  /** True when the choice was parked mid-turn and lands on the next turn. */
  pending?: boolean;
}

/**
 * Read the session's explicit thinking choice and the model's control mode.
 *
 * @param sessionId Session whose flag should be read.
 * @returns The current state; null fields = never set, the backend's
 *          MAIN_LLM_ENABLE_THINKING env default applies.
 */
export async function fetchThinkingState(sessionId: string): Promise<ThinkingState> {
  const res = await fetchApiPayload<ThinkingState & { success?: boolean }>({
    url: '/sessions/thinking',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return {
    mode: res.mode ?? 'on_off',
    enabled: res.enabled ?? null,
    level: res.level ?? null,
    pending: res.pending === true
  };
}

/**
 * Persist the session's explicit thinking choice.
 *
 * Switching is allowed at any moment: while a turn is in flight the choice is
 * parked and lands on the next turn (the running turn keeps its variant).
 *
 * @param sessionId Session whose flag should be written.
 * @param value Boolean for on_off models; 'low' | 'high' | 'max' for level models.
 * @returns Whether the choice was parked for the next turn.
 */
export async function setThinkingValue(sessionId: string, value: ThinkingValue): Promise<{ pending: boolean }> {
  const res = await fetchApiPayload<{ pending?: boolean }>({
    url: '/sessions/thinking',
    opts: { session_id: sessionId, value },
    method: 'put'
  });
  return { pending: res.pending === true };
}

/** Session project directory: where the tools resolve relative paths against. */
export interface ProjectDirectoryState {
  /** The session's own binding; null = unbound (the process default applies). */
  directory: string | null;
  /** Where the tools actually resolve right now (binding, or the default). */
  effective: string;
  /** 'session' | 'env' | 'default' — which of the two is in effect. */
  source: string;
  /** A choice parked for the next turn boundary, or null. */
  pendingDirectory: string | null;
}

/**
 * Read the session's project directory.
 *
 * @param sessionId Session whose binding should be read.
 */
export async function fetchProjectDirectory(sessionId: string): Promise<ProjectDirectoryState> {
  const res = await fetchApiPayload<{
    directory?: string | null;
    effective?: string;
    source?: string;
    pending_directory?: string | null;
  }>({
    url: '/sessions/project',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return {
    directory: res.directory ?? null,
    effective: res.effective ?? '',
    source: res.source ?? 'default',
    pendingDirectory: res.pending_directory ?? null
  };
}

/**
 * Bind (or clear, with ``null``) the session's project directory.
 *
 * While a turn is in flight the choice is parked and lands on the next turn;
 * the response reports ``pending`` so the control can show the clock icon.
 *
 * @param sessionId Session whose binding should be written.
 * @param directory Absolute path, or null to unbind.
 */
export async function setProjectDirectory(
  sessionId: string,
  directory: string | null
): Promise<{ ok: boolean; state: ProjectDirectoryState; pending: boolean }> {
  const res = await fetchApiPayload<{
    ok?: boolean;
    directory?: string | null;
    effective?: string;
    source?: string;
    pending?: boolean;
  }>({
    url: '/sessions/project',
    opts: { session_id: sessionId, directory },
    method: 'put'
  });
  return {
    ok: res.ok !== false,
    pending: res.pending === true,
    state: {
      directory: res.directory ?? null,
      effective: res.effective ?? '',
      source: res.source ?? 'default',
      pendingDirectory: res.pending ? (res.directory ?? null) : null
    }
  };
}

/**
 * Main-model override payload shapes.
 *
 * The override mirrors one entry of the environment-config MAIN_LLM list (a
 * saved profile: provider/model/base_url/api_key). The session runs on that
 * model from the NEXT turn on; the stored credential never echoes back —
 * ``has_api_key`` reports whether one is held.
 */
export interface SessionModelProfile {
  /** Client profile id (stable across the picker's entries). */
  id: string;
  /** Display label of the profile. */
  label: string;
  /** Provider id (`openai`, `zhipu`, ...); absent = keep the env provider. */
  provider?: string;
  /** Model / API name the backend calls. */
  model: string;
  /** Gateway base URL; absent = keep the env one. */
  base_url?: string;
  /** Gateway credential; absent = keep the env one. */
  api_key?: string;
}

/** A stored override as returned by the backend (credential masked). */
export type SessionModelOverride = Omit<SessionModelProfile, 'api_key'> & {
  /** Whether the stored override carries a credential. */
  has_api_key?: boolean;
};

export interface SessionModelState {
  /** The session's override, or null while it follows the env config. */
  override: SessionModelOverride | null;
  /** The env-configured identity the "follow env config" entry stands for. */
  env_model: { provider: string | null; model: string | null };
  /** True when the choice was parked mid-turn and lands on the next turn. */
  pending?: boolean;
}

/**
 * Read the session's main-model override and the env-configured identity.
 *
 * @param sessionId Session whose override should be read.
 * @returns The current state; a null override = follow the environment config.
 */
export async function fetchSessionModel(sessionId: string): Promise<SessionModelState> {
  const res = await fetchApiPayload<SessionModelState & { success?: boolean }>({
    url: '/sessions/model',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return {
    override: res.override ?? null,
    env_model: res.env_model ?? { provider: null, model: null },
    pending: res.pending === true
  };
}

/**
 * Persist (or clear, with null) the session's main-model override.
 *
 * @param sessionId Session whose model should change.
 * @param profile The chosen env-config profile, or null to follow the env config.
 * @returns The state the backend stored (override masked), for the control to mirror.
 */
export async function setSessionModel(
  sessionId: string,
  profile: SessionModelProfile | null
): Promise<SessionModelState> {
  const res = await fetchApiPayload<SessionModelState & { success?: boolean }>({
    url: '/sessions/model',
    opts: { session_id: sessionId, profile },
    method: 'put'
  });
  return {
    override: res.override ?? null,
    env_model: res.env_model ?? { provider: null, model: null },
    pending: res.pending === true
  };
}
