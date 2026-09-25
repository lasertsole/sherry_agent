/**
 * Scroll-up history pagination for the chat list.
 *
 * The list opens on the newest page (latest turns); when the user pulls up to
 * the top, this controller fetches the next older turn window and hands the
 * rows to the page's merge callback. The virtualizer's end anchoring keeps the
 * viewport stable across the prepend, so loading older history never jumps the
 * reading position.
 *
 * Guard rails:
 *  - one in-flight request at a time (`loadingOlder`);
 *  - `exhausted` is set when a page comes back empty, when the window reaches
 *    turn 1, or when a page adds no turn older than the current oldest — a
 *    FAILED request never marks exhaustion (it stays retryable);
 *  - a session switch resets both states.
 *
 * @module composables/use-chat-older-history
 */
import type { MessageItem } from '~/pages/home/type';
// Explicit sibling imports (not the auto-imported bare symbols) so tests can
// vi.mock the bridge with a stable module specifier; the unimport injection is
// compile-time and leaves bare symbols unmockable under Vitest.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { toMessageItems } from './message-items';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { get_older_history_page } from './messages';

/** Turns requested per scroll-up page (mirrors the initial load's 10). */
export const OLDER_HISTORY_PAGE_TURNS = 10;

export interface OlderHistoryOptions {
  /** Getter for the live session id. */
  sessionId: () => string;
  /** Getter for the currently loaded messages (used to find the oldest turn). */
  messages: () => MessageItem[];
  /** Merge the prepended rows into the page's message list (page owns ordering). */
  prepend: (rows: MessageItem[]) => void;
}

/**
 * Create the scroll-up history controller.
 * @param options
 */
export function useChatOlderHistory(options: OlderHistoryOptions) {
  /** A page request is in flight (drives the top loading indicator). */
  const loadingOlder = ref(false);
  /** The session start has been reached — no further requests. */
  const exhausted = ref(false);

  /** Oldest positive turn currently loaded (0 when none). */
  const oldestTurn = computed<number>(() => {
    let min = 0;
    for (const m of options.messages()) {
      if (m.turn_num > 0 && (min === 0 || m.turn_num < min)) min = m.turn_num;
    }
    return min;
  });

  /** Newest positive turn currently loaded (0 when none) — the server's page anchor. */
  const newestTurn = computed<number>(() => {
    let max = 0;
    for (const m of options.messages()) {
      if (m.turn_num > max) max = m.turn_num;
    }
    return max;
  });

  // Session switch: the loaded window starts over.
  watch(
    () => options.sessionId(),
    () => {
      exhausted.value = false;
      loadingOlder.value = false;
    }
  );

  /** Fetch the next older page (no-op while loading, exhausted, or at turn 1). */
  const loadOlder = async (): Promise<void> => {
    const sid = options.sessionId();
    const oldest = oldestTurn.value;
    if (!sid || loadingOlder.value || exhausted.value) return;
    if (oldest <= 1) {
      exhausted.value = true;
      return;
    }

    loadingOlder.value = true;
    try {
      const page = await get_older_history_page(sid, oldest, newestTurn.value, OLDER_HISTORY_PAGE_TURNS);
      if (page === null) return; // request failed: keep it retryable
      if (!page.rows.length) {
        exhausted.value = true;
        return;
      }
      const rows = toMessageItems(page.rows);
      options.prepend(rows);
      // A partial window, the session start, or a page that added nothing
      // older than what we already had all mean "no more pages".
      if (page.exhausted || oldestTurn.value >= oldest) exhausted.value = true;
    } finally {
      loadingOlder.value = false;
    }
  };

  return { loadingOlder, exhausted, oldestTurn, loadOlder };
}
