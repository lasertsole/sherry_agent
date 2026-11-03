import { defineStore } from 'pinia';
import { ref } from 'vue';

/**
 * Session ids pinned to the top of the left sidebar.
 *
 * The pin is a local UI preference, not server session data: it never
 * round-trips through the session API and a session can be pinned without
 * changing its record. Persisted through the Pinia persisted-state plugin (the
 * same store shape the sidebar width uses), so a reload keeps the order.
 */
export const usePinnedSessionsStore = defineStore(
  'pinnedSessions',
  () => {
    /** Pinned ids in pin order (the order itself is not displayed; pinned rows keep list order). */
    const ids = ref<string[]>([]);

    /**
     * Whether a session is pinned.
     * @param id Session id.
     * @returns True when the row sorts to the top.
     */
    const has = (id: string): boolean => ids.value.includes(id);

    /**
     * Pin or unpin one session.
     * @param id Session id.
     */
    const toggle = (id: string): void => {
      ids.value = has(id) ? ids.value.filter(item => item !== id) : [...ids.value, id];
    };

    /**
     * Forget a deleted session.
     * @param id Session id.
     */
    const remove = (id: string): void => {
      if (has(id)) ids.value = ids.value.filter(item => item !== id);
    };

    return { ids, has, toggle, remove };
  },
  {
    persist: {
      pick: ['ids']
    }
  }
);
