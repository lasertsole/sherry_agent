/**
 * In-conversation text search (the chat's Ctrl+Shift+F find bar).
 *
 * The search runs over the LOADED message array (`chatMessages`), never the
 * DOM: a turn's tool cards / intermediate text are folded away and virtual rows
 * outside the window are not mounted at all, so the in-memory list is the only
 * complete view of what a reader can jump to. Every match carries the message
 * id plus the character offset inside that message's content, which is what
 * lets the caller line the occurrence up with the row it must reveal.
 *
 * Pure text matching, case-insensitive, plain substring (no regex): the plan's
 * MVP, and the reason a query cannot throw.
 *
 * @module composables/use-conversation-find
 */
import { computed, ref } from 'vue';

import type { MessageItem } from '~/pages/home/type';

/** One occurrence of the query inside a message. */
export interface FindMatch {
  /** `MessageItem.id` of the row holding the occurrence. */
  messageId: number;
  /** Offset of the occurrence inside the message's content. */
  charIndex: number;
  /** Length of the occurrence (= the query's length). */
  length: number;
}

/**
 * Create the find controller for one conversation.
 * @param messages Getter returning the currently LOADED messages, in render order.
 */
export function useConversationFind(messages: () => MessageItem[]) {
  /** Current query as typed (not trimmed: the bar shows what the user wrote). */
  const query = ref('');
  /** Every occurrence of the current query, in message order then offset order. */
  const matches = ref<FindMatch[]>([]);
  /** Index of the match the ring is on (-1 = none). */
  const currentIndex = ref(-1);
  /** Whether the find bar is open. */
  const visible = ref(false);

  /** The match under the ring, or null. */
  const current = computed<FindMatch | null>(() =>
    currentIndex.value >= 0 ? (matches.value[currentIndex.value] ?? null) : null
  );

  /** Total occurrences (the bar's denominator). */
  const total = computed(() => matches.value.length);

  /**
   * Recompute the match list for a query and select the first hit.
   *
   * A blank query clears everything (the bar keeps its input, the caller shows
   * 0/0). Matching is per message and per occurrence, so one message can hold
   * several matches and the count reflects that.
   * @param next The query as typed.
   */
  function search(next: string): void {
    query.value = next;
    const needle = next.trim().toLowerCase();
    if (!needle) {
      matches.value = [];
      currentIndex.value = -1;
      return;
    }
    const found: FindMatch[] = [];
    for (const message of messages()) {
      const content = message.content ?? '';
      if (!content) continue;
      const haystack = content.toLowerCase();
      let at = haystack.indexOf(needle);
      while (at >= 0) {
        found.push({ messageId: message.id, charIndex: at, length: needle.length });
        at = haystack.indexOf(needle, at + needle.length);
      }
    }
    matches.value = found;
    currentIndex.value = found.length > 0 ? 0 : -1;
  }

  /** Move to the next occurrence (wraps around). */
  function next(): void {
    if (matches.value.length === 0) return;
    currentIndex.value = (currentIndex.value + 1) % matches.value.length;
  }

  /** Move to the previous occurrence (wraps around). */
  function previous(): void {
    if (matches.value.length === 0) return;
    currentIndex.value = (currentIndex.value - 1 + matches.value.length) % matches.value.length;
  }

  /** Drop the query and its matches (the bar stays open). */
  function clear(): void {
    query.value = '';
    matches.value = [];
    currentIndex.value = -1;
  }

  /** Open the bar (the caller focuses the input). */
  function open(): void {
    visible.value = true;
  }

  /** Close the bar and forget the query. */
  function close(): void {
    visible.value = false;
    clear();
  }

  return { query, matches, currentIndex, visible, current, total, search, next, previous, clear, open, close };
}
