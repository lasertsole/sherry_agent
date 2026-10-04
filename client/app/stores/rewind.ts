/**
 * Conversation-rewind state for one session: whether the conversation can be
 * cut back right now, and the call that does it.
 *
 * There is no push frame for this (a rewind is a user action, not agent
 * output): the store asks on every socket open — the same recovery moment the
 * other stores use — and refreshes after its own successful cut. The server's
 * verdict is what greys the buttons out while a turn is running.
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { sessionIdFromPathname } from '../utils/session-route';
// Explicit import: unimport's walk of `app/composables` does not reach the
// `bridge/` subdirectory (the same gap `use-chat-stream.ts` documents for
// `fetchTurnState`), so the bare symbol would be undefined under Vitest.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchRewindState, postRewind } from '~/composables/bridge/session';

export const useRewindStore = defineStore('rewind', () => {
  const canRewind = ref(false);
  const generation = ref(0);
  /** The id ranges the active branch hides: (after_id, up_to_id]. */
  const hiddenRanges = ref<Array<[number, number]>>([]);
  const currentSid = ref('');
  const rewinding = ref(false);
  const subscribed = ref(false);

  /** The control is offered only when the server says the cut is allowed. */
  const enabled = computed(() => canRewind.value && !rewinding.value);

  /** Resolve the session id from the route when the caller has none. */
  function resolveSid(): string {
    if (typeof window === 'undefined') return '';
    return sessionIdFromPathname(window.location.pathname) ?? '';
  }

  /**
   * Ask the server for the current verdict.
   * @param sid
   */
  async function refresh(sid?: string): Promise<void> {
    const target = sid || currentSid.value || resolveSid();
    currentSid.value = target;
    if (!target) return;
    // Fail-open: a failed probe (backend down, a test's stubbed fetch) keeps
    // the last known verdict instead of leaving an unhandled rejection behind
    // — the control greys out only when the server explicitly says so.
    const state = await fetchRewindState(target).catch(() => null);
    if (!state) return;
    canRewind.value = state.can_rewind;
    generation.value = state.branch_generation;
    hiddenRanges.value = state.hidden_ranges;
  }

  /** Register the refresh-on-connect listener once (singleton guard). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:connected', () => {
      void refresh();
    });
  }

  /**
   * Is this message id on a branch the user cut away (hidden from the view)?
   * @param messageId
   */
  function isHidden(messageId: number): boolean {
    return hiddenRanges.value.some(([after, upTo]) => messageId > after && messageId <= upTo);
  }

  /**
   * Cut the conversation back to a message.
   * @param cutAfterMessageId The last message the branch keeps.
   * @returns Whether the cut was applied.
   */
  async function rewind(cutAfterMessageId: number): Promise<boolean> {
    const sid = currentSid.value || resolveSid();
    if (!sid) return false;
    rewinding.value = true;
    try {
      const result = await postRewind(sid, cutAfterMessageId);
      if (result.ok) await refresh(sid);
      return result.ok;
    } finally {
      rewinding.value = false;
    }
  }

  return {
    canRewind,
    enabled,
    isHidden,
    hiddenRanges,
    generation,
    currentSid,
    rewinding,
    subscribed,
    refresh,
    subscribe,
    rewind
  };
});
