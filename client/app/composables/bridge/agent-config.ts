/**
 * Per-session agent configuration bridge (预设 的工具 / 中间件 / 子代理模型 三栏).
 *
 * Three calls, mirroring `GET /agent/catalog` and `GET|PUT /sessions/agent_config`:
 *
 * - {@link fetchAgentCatalog} — the tool / middleware / subagent-role lists the
 *   panel renders. The client never hardcodes backend names; this is the source.
 * - {@link fetchAgentConfig} / {@link setAgentConfig} — the session's own
 *   selection. A write while a turn is in flight is PARKED by the backend and
 *   applies from the next turn (`pending: true`), exactly like the model and
 *   thinking controls.
 *
 * @module bridge/agent-config
 */
// ``fetchApiPayload`` is auto-imported from ~/composables/requestApi (the bridge
// convention: no explicit import of an auto-imported symbol).
import type { SessionModelProfile } from './session';

/** One selectable main-agent tool. */
export interface AgentToolEntry {
  name: string;
  /** Group id (`files`, `terminal`, `tasks`, ...) — the UI's section key. */
  group: string;
  /**
   * The tool's own first description line (english, served by the backend) —
   * shown as a hover tooltip so a long catalogue stays readable.
   */
  description?: string;
  /** Locked on: a session config may not drop it (the backend refuses one that does). */
  required?: boolean;
}

/** One middleware of the main chain. */
export interface AgentMiddlewareEntry {
  name: string;
  /** Locked on: the safety baseline or a logical necessity (prompt injector). */
  required: boolean;
  /** Whether a session may turn it off. */
  gateable: boolean;
}

/** One functional subagent role. */
export interface AgentRoleEntry {
  role: string;
  /** The role's default tier; null = no definition (depth-based default). */
  model_tier: string | null;
  description: string;
}

export interface AgentCatalog {
  tools: AgentToolEntry[];
  middlewares: AgentMiddlewareEntry[];
  subagent_roles: AgentRoleEntry[];
}

/**
 * The per-session payload. Absent fields mean "no opinion": every tool on,
 * every middleware on, every role on its tier.
 */
export interface AgentConfig {
  /** Enabled tool names, or null/absent for every tool. */
  tools?: string[] | null;
  /** Middleware names turned OFF (gateable entries only). */
  middlewares_disabled?: string[];
  /** Per-role model profiles; null = follow the role's tier. */
  subagent_models?: Record<string, SessionModelProfile | null>;
}

export interface AgentConfigState {
  config: AgentConfig;
  /** True when the write was parked mid-turn and lands on the next turn. */
  pending: boolean;
}

/** Fetch the tool / middleware / role catalogue (one call, three lists). */
export async function fetchAgentCatalog(): Promise<AgentCatalog> {
  const res = await fetchApiPayload<AgentCatalog & { success?: boolean }>({
    url: '/agent/catalog',
    method: 'get'
  });
  return {
    tools: res.tools ?? [],
    middlewares: res.middlewares ?? [],
    subagent_roles: res.subagent_roles ?? []
  };
}

/**
 * Read the session's effective agent config (a parked choice wins).
 * @param sessionId
 */
export async function fetchAgentConfig(sessionId: string): Promise<AgentConfigState> {
  const res = await fetchApiPayload<AgentConfigState & { success?: boolean }>({
    url: '/sessions/agent_config',
    opts: { session_id: sessionId },
    method: 'get'
  });
  return { config: res.config ?? {}, pending: res.pending === true };
}

/**
 * Write the session's agent config (live, or parked while a turn runs).
 * @param sessionId
 * @param config
 */
export async function setAgentConfig(sessionId: string, config: AgentConfig): Promise<AgentConfigState> {
  const res = await fetchApiPayload<AgentConfigState & { success?: boolean }>({
    url: '/sessions/agent_config',
    opts: { session_id: sessionId, config },
    method: 'put'
  });
  return { config: res.config ?? {}, pending: res.pending === true };
}
