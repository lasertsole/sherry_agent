import { defineStore } from 'pinia';

/**
 * MAIN_LLM model profiles for the environment-config panel.
 *
 * The `.env` file holds ONE active MAIN_LLM_* set, so multiple models are a
 * client-side concept: each profile stores a named parameter set, "save"
 * persists the edited parameters, and "apply" writes the profile into `.env`
 * (through the dialog's existing PUT /env flow) and marks it active.
 *
 * Persisted to localStorage (same pinia-plugin-persistedstate pattern as the
 * UI store) so the list and the active marker survive reloads; the active
 * marker is re-derived against the live `.env` values by the panel, so a
 * manually edited .env always wins over a stale marker.
 */
export interface LlmProfile {
  /** Stable local id. */
  id: string;
  /** Display name (seeded from MAIN_LLM_NAME at creation). */
  label: string;
  /** MAIN_LLM_* key → value. Only keys present in `.env` are ever stored/applied. */
  params: Record<string, string>;
}

export const useLlmProfilesStore = defineStore(
  'llmProfiles',
  () => {
    /** Saved model profiles, in insertion order. */
    const profiles = ref<LlmProfile[]>([]);
    /** Id of the profile last applied to `.env` (null until one is applied). */
    const activeId = ref<string | null>(null);

    /**
     * Append a profile and return its id.
     * @param label Display name.
     * @param params MAIN_LLM_* parameter set.
     */
    const add = (label: string, params: Record<string, string>): string => {
      const id =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : `p-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      profiles.value = [...profiles.value, { id, label, params: { ...params } }];
      return id;
    };

    /**
     * Replace a profile's name and/or parameter set.
     * @param id
     * @param patch
     */
    const update = (id: string, patch: Partial<Omit<LlmProfile, 'id'>>): void => {
      profiles.value = profiles.value.map(p =>
        p.id === id ? { ...p, ...patch, params: patch.params ? { ...patch.params } : p.params } : p
      );
    };

    /**
     * Remove a profile (clears the active marker when it pointed at it).
     * @param id
     */
    const remove = (id: string): void => {
      profiles.value = profiles.value.filter(p => p.id !== id);
      if (activeId.value === id) activeId.value = null;
    };

    /**
     * Mark a profile as the one applied to `.env`.
     * @param id
     */
    const setActive = (id: string | null): void => {
      activeId.value = id;
    };

    /**
     * Look a profile up by id.
     * @param id
     */
    const byId = (id: string | null): LlmProfile | undefined =>
      id === null ? undefined : profiles.value.find(p => p.id === id);

    /** Test seam: drop all client-side profiles and the active marker. */
    const _resetForTest = (): void => {
      profiles.value = [];
      activeId.value = null;
    };

    return { profiles, activeId, add, update, remove, setActive, byId, _resetForTest };
  },
  { persist: { pick: ['profiles', 'activeId'] } }
);
