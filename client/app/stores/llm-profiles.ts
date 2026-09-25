import { defineStore } from 'pinia';

/**
 * Per-group model profiles for the environment-config panel.
 *
 * Every model family in `.env` (MAIN_LLM / REASONER_LLM / AUXILIARY_LLM /
 * ITTT / VTTT / TTI / RERANKER / EMBEDDING / STT) holds ONE active key set, so
 * multiple models per family are a client-side concept: each profile stores a
 * named parameter set for its group, "save" persists the edited parameters,
 * and "apply" writes the profile into `.env` (through the dialog's existing
 * PUT /env flow) and marks it active for that group.
 */
export interface LlmProfile {
  /** Stable local id. */
  id: string;
  /** Display name (seeded from the group's name key at creation). */
  label: string;
  /** Group key → value (e.g. `MAIN_LLM_NAME` → `glm-4.6`). Only keys present in `.env` are stored/applied. */
  params: Record<string, string>;
  /**
   * Creation time (epoch ms) — the ordering key for the per-group cap. Older
   * payloads lack it; those entries fall back to their array index, which is
   * also creation order because `add` appends.
   */
  createdAt?: number;
}

/** Maximum saved models per group (the built-in local entry does not count). */
export const MAX_PROFILES_PER_GROUP = 15;

export const useLlmProfilesStore = defineStore(
  'llmProfiles',
  () => {
    /** Group name → profiles, in insertion order. */
    const byGroup = ref<Record<string, LlmProfile[]>>({});
    /** Group name → id of the profile last applied to `.env`. */
    const activeByGroup = ref<Record<string, string | null>>({});
    /**
     * Profiles of one group ([] when the group has none).
     * @param group
     */
    const listFor = (group: string): LlmProfile[] => byGroup.value[group] ?? [];

    /**
     * Active profile id of one group (null when none applied).
     * @param group
     */
    const activeIdFor = (group: string): string | null => activeByGroup.value[group] ?? null;

    /**
     * Append a profile to a group and return its id.
     * @param group Group name (e.g. `MAIN_LLM`).
     * @param label Display name.
     * @param params Parameter set for the group.
     */
    const add = (group: string, label: string, params: Record<string, string>): string | null => {
      // Hard cap at the data layer too: the UI disables the button, but a
      // racing click (or a direct store call) must never exceed the limit.
      if (listFor(group).length >= MAX_PROFILES_PER_GROUP) return null;
      const id =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : `p-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      byGroup.value = {
        ...byGroup.value,
        [group]: [...listFor(group), { id, label, params: { ...params }, createdAt: Date.now() }]
      };
      return id;
    };

    /**
     * Enforce the per-group cap on already-stored data (older payloads or a
     * hand-edited localStorage can exceed it). Truncation is by CREATION TIME:
     * the oldest `MAX_PROFILES_PER_GROUP` survive and the newest overflow is
     * dropped, from both the reactive list and the persisted state (the
     * persistence plugin writes whenever this method changes the state).
     * @param group
     * @returns Number of dropped profiles (0 when nothing had to go).
     */
    const trimGroup = (group: string): number => {
      const list = listFor(group);
      if (list.length <= MAX_PROFILES_PER_GROUP) return 0;
      const ordered = list
        .map((profile, index) => ({ profile, rank: profile.createdAt ?? index }))
        .sort((a, b) => a.rank - b.rank);
      const kept = ordered.slice(0, MAX_PROFILES_PER_GROUP).map(entry => entry.profile);
      const dropped = ordered.slice(MAX_PROFILES_PER_GROUP);
      byGroup.value = { ...byGroup.value, [group]: kept };
      // Never leave the active marker pointing at a dropped profile.
      const droppedIds = new Set(dropped.map(entry => entry.profile.id));
      if (droppedIds.has(activeIdFor(group) ?? '')) {
        activeByGroup.value = {
          ...activeByGroup.value,
          [group]: kept[kept.length - 1]?.id ?? null
        };
      }
      return dropped.length;
    };

    /**
     * Replace a profile's name and/or parameter set within its group.
     * @param group
     * @param id
     * @param patch
     */
    const update = (group: string, id: string, patch: Partial<Omit<LlmProfile, 'id'>>): void => {
      byGroup.value = {
        ...byGroup.value,
        [group]: listFor(group).map(p =>
          p.id === id ? { ...p, ...patch, params: patch.params ? { ...patch.params } : p.params } : p
        )
      };
    };

    /**
     * Remove a profile from its group (clears the group's active marker when it pointed there).
     * @param group
     * @param id
     */
    const remove = (group: string, id: string): void => {
      byGroup.value = { ...byGroup.value, [group]: listFor(group).filter(p => p.id !== id) };
      if (activeIdFor(group) === id) {
        activeByGroup.value = { ...activeByGroup.value, [group]: null };
      }
    };

    /**
     * Mark a profile as the one applied to `.env` for its group.
     * @param group
     * @param id
     */
    const setActive = (group: string, id: string | null): void => {
      activeByGroup.value = { ...activeByGroup.value, [group]: id };
    };

    /**
     * Look a profile up by id within a group.
     * @param group
     * @param id
     */
    const byId = (group: string, id: string | null): LlmProfile | undefined =>
      id === null ? undefined : listFor(group).find(p => p.id === id);

    /** Test seam: drop all client-side profiles and active markers. */
    const _resetForTest = (): void => {
      byGroup.value = {};
      activeByGroup.value = {};
    };

    return {
      byGroup,
      activeByGroup,
      listFor,
      activeIdFor,
      add,
      trimGroup,
      update,
      remove,
      setActive,
      byId,
      _resetForTest
    };
  },
  { persist: { pick: ['byGroup', 'activeByGroup'] } }
);
