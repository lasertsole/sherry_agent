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
}

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
    const add = (group: string, label: string, params: Record<string, string>): string => {
      const id =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : `p-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      byGroup.value = {
        ...byGroup.value,
        [group]: [...listFor(group), { id, label, params: { ...params } }]
      };
      return id;
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
      update,
      remove,
      setActive,
      byId,
      _resetForTest
    };
  },
  { persist: { pick: ['byGroup', 'activeByGroup'] } }
);
