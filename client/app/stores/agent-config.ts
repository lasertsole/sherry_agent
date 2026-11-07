import { defineStore } from 'pinia';
import type { AgentCatalog, AgentConfig, AgentRoleEntry, AgentSkillEntry } from '~/composables/bridge/agent-config';
// Stable module specifiers so tests can vi.mock the bridge; the unimport
// injection is compile-time and leaves bare symbols unmockable.
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { fetchAgentCatalog, fetchAgentConfig, setAgentConfig } from '~/composables/bridge/agent-config';
/* eslint-enable @typescript-eslint/no-restricted-imports */

/**
 * Sentinel id of the "follow the role's model_tier" option. A NON-empty value on
 * purpose: PrimeVue's Select renders an empty-string value as an empty box, so the
 * picker would look unset instead of showing its default entry.
 */
export const FOLLOW_TIER_ID = 'tier';

/**
 * The state of the preset panel's Agent models / Skills tabs.
 *
 * Two halves:
 *
 * - the CATALOG (subagent roles, the skill list split Built-in / Third-party) is
 *   process-wide and fetched once — the client never hardcodes backend names;
 * - the CONFIG is per session and mirrored from the backend on tab open; a save
 *   writes the whole payload back (`PUT /sessions/agent_config`), which lands
 *   live or is parked until the turn boundary (`pending`).
 *
 * A missing config key means "no opinion": every role on its `model_tier`,
 * every skill in the index — the backend contract the payload mirrors. Built-in
 * tools and the middleware chain are not configurable at all.
 */
export const useAgentConfigStore = defineStore('agentConfig', () => {
  /** The role / skill lists (empty until the first load). */
  const catalog = ref<AgentCatalog>({ subagent_roles: [], skills: [] });
  /** True once the catalogue has been fetched (success or not). */
  const catalogLoaded = ref(false);
  /** sid → the session's own config as the backend reports it. */
  const bySession = ref<Record<string, AgentConfig>>({});
  /** sid → the last write was parked mid-turn and lands on the next turn. */
  const pendingBySession = ref<Record<string, boolean>>({});
  /** sid → the session has been hydrated at least once. */
  const hydrated = ref<Record<string, boolean>>({});

  /** The subagent roles in catalogue order. */
  const subagentRoles = computed<AgentRoleEntry[]>(() => catalog.value.subagent_roles);

  /** The skills split into the two Skills sub-tabs (Built-in / Third-party), each by name. */
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
   * The session's skill selection, expanded to ALL when it has no opinion.
   * An EMPTY selection is a real choice ("no skill in the index"), so the
   * caller must be able to tell it apart from "no opinion" (``null``).
   * @param sessionId
   */
  function selectedSkills(sessionId: string): string[] | null {
    const configured = configOf(sessionId).skills;
    if (!Array.isArray(configured)) return null;
    const wanted = new Set(configured);
    // The REQUIRED chain is always in: the backend refuses a stored list that
    // omits one, so a legacy value reads as the effective set.
    return catalog.value.skills
      .filter(skill => wanted.has(skill.name) || skill.required === true)
      .map(skill => skill.name);
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
    subagentRoles,
    skills,
    loadCatalog,
    hydrate,
    configOf,
    selectedSkills,
    isPending,
    save
  };
});
