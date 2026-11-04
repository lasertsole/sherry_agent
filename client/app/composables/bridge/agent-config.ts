/**
 * Per-session agent configuration bridge (the preset's Agent models / Skills tabs).
 *
 * Three calls, mirroring `GET /agent/catalog` and `GET|PUT /sessions/agent_config`:
 *
 * - {@link fetchAgentCatalog} — the subagent-role / skill lists the panel
 *   renders. The client never hardcodes backend names.
 * - {@link fetchAgentConfig} / {@link setAgentConfig} — the session's own
 *   selection. A write while a turn is in flight is PARKED by the backend and
 *   applies from the next turn (`pending: true`), exactly like the model and
 *   thinking controls.
 *
 * The built-in tool set and the middleware chain are NOT configurable: every
 * main tool is always available and every registered middleware always runs,
 * so neither appears in the catalogue or the payload.
 *
 * @module bridge/agent-config
 */
// ``fetchApiPayload`` is auto-imported from ~/composables/requestApi (the bridge
// convention: no explicit import of an auto-imported symbol).
import type { SessionModelProfile } from './session';

/** One skill the Skills tab offers (the ``<available_skills>`` index's members). */
export interface AgentSkillEntry {
  name: string;
  /** The SKILL.md description (bounded) — the row's hover tooltip. */
  description?: string;
  /** Trigger conditions from the frontmatter (bounded) — hover tooltip too. */
  when_to_use?: string;
  /** Exclusion conditions from the frontmatter (bounded; optional field). */
  when_not_to_use?: string;
  /** False = an uploaded third-party skill (`skills/plugins/`). */
  builtin: boolean;
  /** Locked on: the multimedia chain, which no session config may drop. */
  required?: boolean;
}

/** One functional subagent role. */
export interface AgentRoleEntry {
  role: string;
  /** The role's default tier; null = no definition (depth-based default). */
  model_tier: string | null;
  description: string;
}

export interface AgentCatalog {
  subagent_roles: AgentRoleEntry[];
  skills: AgentSkillEntry[];
}

/**
 * The per-session payload. Absent fields mean "no opinion": every role on its
 * tier, every skill in the index.
 */
export interface AgentConfig {
  /** Per-role model profiles; null = follow the role's tier. */
  subagent_models?: Record<string, SessionModelProfile | null>;
  /**
   * Skill names whose index entries enter the system prompt, or null/absent for
   * every skill. An EMPTY list is a legal, explicit choice — nothing beyond the
   * required multimedia chain enters the index.
   */
  skills?: string[] | null;
}

export interface AgentConfigState {
  config: AgentConfig;
  /** True when the write was parked mid-turn and lands on the next turn. */
  pending: boolean;
}

/** Fetch the role / skill catalogue (one call, two lists). */
export async function fetchAgentCatalog(): Promise<AgentCatalog> {
  const res = await fetchApiPayload<AgentCatalog & { success?: boolean }>({
    url: '/agent/catalog',
    method: 'get'
  });
  return {
    subagent_roles: res.subagent_roles ?? [],
    skills: res.skills ?? []
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
