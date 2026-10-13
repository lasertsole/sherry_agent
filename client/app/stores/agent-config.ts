import { defineStore } from 'pinia';
import type {
  AgentCatalog,
  AgentConfig,
  AgentMiddlewareEntry,
  AgentRoleEntry,
  AgentSkillEntry,
  AgentToolEntry
} from '~/composables/bridge/agent-config';
// Stable module specifiers so tests can vi.mock the bridge; the unimport
// injection is compile-time and leaves bare symbols unmockable.
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { fetchAgentCatalog, fetchAgentConfig, setAgentConfig } from '~/composables/bridge/agent-config';
/* eslint-enable @typescript-eslint/no-restricted-imports */

/**
 * The 预设-工具/中间件/子代理模型 three tabs' state.
 *
 * Two halves:
 *
 * - the CATALOG (tools grouped, middleware lock flags, subagent roles, the skill
 *   list split 内置 / 第三方) is
 *   process-wide and fetched once — the client never hardcodes backend names;
 * - the CONFIG is per session and mirrored from the backend on tab open; a save
 *   writes the whole payload back (`PUT /sessions/agent_config`), which lands
 *   live or is parked until the turn boundary (`pending`).
 *
 * A missing config key means "no opinion": every tool on, every middleware on,
 * every role on its `model_tier` — the backend contract the payload mirrors.
 */
/**
 * Sentinel id of the "follow the role's model_tier" option. A NON-empty value on
 * purpose: PrimeVue's Select renders an empty-string value as an empty box, so the
 * picker would look unset instead of showing its default entry.
 */
export const FOLLOW_TIER_ID = 'tier';

/**
 * Groups the 工具 tab offers as select-all / clear-all only — their membership
 * moves together (the backend owns the list: ``agent.tools.catalog.BULK_ONLY_GROUPS``,
 * mirrored here because the catalogue response itself is per-tool).
 */
export const BULK_ONLY_TOOL_GROUPS = ['tasks', 'subagents'] as const;

/**
 * Whether a group is bulk-only (no per-tool switches in the UI).
 * @param group Group id.
 */
export function isBulkOnlyToolGroup(group: string): boolean {
  return (BULK_ONLY_TOOL_GROUPS as readonly string[]).includes(group);
}

export const useAgentConfigStore = defineStore('agentConfig', () => {
  /** The tool / middleware / role / skill lists (empty until the first load). */
  const catalog = ref<AgentCatalog>({ tools: [], middlewares: [], subagent_roles: [], skills: [] });
  /** True once the catalogue has been fetched (success or not). */
  const catalogLoaded = ref(false);
  /** sid → the session's own config as the backend reports it. */
  const bySession = ref<Record<string, AgentConfig>>({});
  /** sid → the last write was parked mid-turn and lands on the next turn. */
  const pendingBySession = ref<Record<string, boolean>>({});
  /** sid → the session has been hydrated at least once. */
  const hydrated = ref<Record<string, boolean>>({});

  /** Tools grouped by the catalogue's group ids, in first-seen order. */
  const toolGroups = computed<Array<{ group: string; tools: AgentToolEntry[] }>>(() => {
    const groups: Array<{ group: string; tools: AgentToolEntry[] }> = [];
    for (const tool of catalog.value.tools) {
      const bucket = groups.find(entry => entry.group === tool.group);
      if (bucket) bucket.tools.push(tool);
      else groups.push({ group: tool.group, tools: [tool] });
    }
    return groups;
  });

  /** Middleware entries split by the lock flag (the UI renders them apart). */
  const middlewares = computed<{ gateable: AgentMiddlewareEntry[]; locked: AgentMiddlewareEntry[] }>(() => ({
    gateable: catalog.value.middlewares.filter(entry => entry.gateable && !entry.required),
    locked: catalog.value.middlewares.filter(entry => entry.required)
  }));

  /** The subagent roles in catalogue order. */
  const subagentRoles = computed<AgentRoleEntry[]>(() => catalog.value.subagent_roles);

  /** The skills split into the two 技能 sub-tabs (内置 / 第三方), each by name. */
  const skills = computed<{ builtin: AgentSkillEntry[]; thirdParty: AgentSkillEntry[] }>(() => ({
    builtin: catalog.value.skills.filter(skill => skill.builtin),
    thirdParty: catalog.value.skills.filter(skill => !skill.builtin)
  }));

  /**
   * Load the catalogue once (idempotent; a failure leaves it empty and the
   * caller retries on the next open).
   */
  async function loadCatalog(): Promise<void> {
    if (catalogLoaded.value) return;
    const loaded = await fetchAgentCatalog();
    catalog.value = loaded;
    catalogLoaded.value = true;
  }

  /**
   * Pull one session's config (a parked choice wins, as the backend reports it).
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    const state = await fetchAgentConfig(sessionId);
    bySession.value = { ...bySession.value, [sessionId]: state.config };
    pendingBySession.value = { ...pendingBySession.value, [sessionId]: state.pending };
    hydrated.value = { ...hydrated.value, [sessionId]: true };
  }

  /**
   * The session's own config ({} = every default).
   * @param sessionId
   */
  function configOf(sessionId: string): AgentConfig {
    return bySession.value[sessionId] ?? {};
  }

  /**
   * Enabled tool names, expanded to ALL when the session has no opinion. The
   * catalogue's required tools are always unioned in — the backend refuses a
   * stored list that omits one, so a legacy payload still reads as the effective
   * set (in catalogue order).
   * @param sessionId
   */
  function enabledTools(sessionId: string): string[] {
    const configured = configOf(sessionId).tools;
    const selected = new Set(Array.isArray(configured) ? configured : catalog.value.tools.map(tool => tool.name));
    return catalog.value.tools.filter(tool => selected.has(tool.name) || tool.required === true).map(tool => tool.name);
  }

  /**
   * The session's skill selection, expanded to ALL when it has no opinion.
   * Unlike :func:`enabledTools` this returns a plain list: an EMPTY selection is
   * a real choice ("no skill in the index"), so the caller must be able to tell
   * it apart from "no opinion" (``null``).
   * @param sessionId
   */
  function selectedSkills(sessionId: string): string[] | null {
    const configured = configOf(sessionId).skills;
    if (!Array.isArray(configured)) return null;
    const wanted = new Set(configured);
    // The REQUIRED chain is always in, exactly like the required tools: the
    // backend refuses a stored list that omits one, so a legacy value reads as
    // the effective set.
    return catalog.value.skills
      .filter(skill => wanted.has(skill.name) || skill.required === true)
      .map(skill => skill.name);
  }

  /**
   * Whether the summarization nudges run for this session (the one option the
   * required Summarization middleware exposes; default on).
   * @param sessionId
   */
  function nudgeEnabled(sessionId: string): boolean {
    const value = configOf(sessionId).middleware_options?.Summarization?.nudge;
    return typeof value === 'boolean' ? value : true;
  }

  /**
   * Gateable middleware names the session turned OFF ([] = every switch on).
   * @param sessionId
   */
  function disabledMiddlewares(sessionId: string): string[] {
    const disabled = configOf(sessionId).middlewares_disabled;
    return Array.isArray(disabled) ? disabled : [];
  }

  /**
   * Whether the session's choice is parked (lands on the next turn).
   * @param sessionId
   */
  function isPending(sessionId: string): boolean {
    return pendingBySession.value[sessionId] === true;
  }

  /**
   * Write the session's agent config and mirror what the backend stored.
   * @param sessionId
   * @param config
   */
  async function save(sessionId: string, config: AgentConfig): Promise<void> {
    const state = await setAgentConfig(sessionId, config);
    bySession.value = { ...bySession.value, [sessionId]: state.config };
    pendingBySession.value = { ...pendingBySession.value, [sessionId]: state.pending };
  }

  return {
    catalog,
    catalogLoaded,
    bySession,
    pendingBySession,
    hydrated,
    toolGroups,
    middlewares,
    subagentRoles,
    skills,
    loadCatalog,
    hydrate,
    configOf,
    enabledTools,
    selectedSkills,
    nudgeEnabled,
    disabledMiddlewares,
    isPending,
    save
  };
});
