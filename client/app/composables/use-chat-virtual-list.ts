/**
 * Virtualized chat message list (ChatBox): row model + end-anchored virtualizer.
 *
 * Rendering unit = one TURN GROUP (see `use-chat-turn-groups`), so the inner
 * `gap-3` markup stays untouched and only the inter-group spacing moves from
 * the container's `gap-6` to a per-row `padding-bottom`. The window itself is
 * `@tanstack/vue-virtual`, whose chat-specific options cover exactly this
 * list's semantics:
 *  - `anchorTo: 'end'` — prepending older history (pagination) keeps the
 *    visible rows in place instead of jumping;
 *  - `followOnAppend: 'auto'` — streaming output follows only while the user
 *    is already pinned near the bottom (same rule as the old 80px threshold);
 *  - dynamic measurement via `measureElement` — expandable tool cards /
 *    thinking blocks / media re-measure on resize (ResizeObserver).
 *
 * A newly appended USER message always scrolls to the end (the old
 * unconditional-follow rule), via the explicit watch below.
 *
 * @module composables/use-chat-virtual-list
 */
import { useVirtualizer } from '@tanstack/vue-virtual';
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

/** "Near bottom" threshold (px): within this distance the user follows the latest messages */
export const NEAR_BOTTOM_THRESHOLD = 80;

/** First-render height guess per turn group; corrected by real measurement */
const GROUP_ESTIMATE_PX = 160;

/** One virtual row: a whole turn group with a stable identity key. */
export interface ChatVirtualRow {
  /**
   * Stable row key (`g-<first message id>`), required for end-anchored
   * prepend stability — index keys break it.
   */
  key: string;
  /** The turn group this row renders. */
  group: MessageItem[];
}

/**
 * Flatten turn groups into keyed virtual rows.
 * @param groups Turn groups in render order.
 * @returns The virtual row models.
 */
export const buildChatVirtualRows = (groups: MessageItem[][]): ChatVirtualRow[] =>
  groups.map(group => ({ key: `g-${group[0]?.id ?? 'empty'}`, group }));

/**
 * Create the virtual list controller for the chat message list.
 * @param groups Getter returning the turn groups in render order.
 * @param messages Getter returning the raw message list (apps detect user appends).
 */
export function useChatVirtualList(groups: () => MessageItem[][], messages: () => MessageItem[] | undefined) {
  /** Chat list scroll container (the outermost overflow-auto div) */
  const scrollContainerRef = useTemplateRef<HTMLDivElement>('scrollContainerRef');

  const rows = computed<ChatVirtualRow[]>(() => buildChatVirtualRows(groups()));

  const virtualizer = useVirtualizer({
    // Getters keep the options reactive: the adapter spreads this object inside
    // a computed and re-applies it whenever a tracked value changes.
    get count() {
      return rows.value.length;
    },
    getScrollElement: () => scrollContainerRef.value,
    estimateSize: () => GROUP_ESTIMATE_PX,
    getItemKey: (index: number) => rows.value[index]?.key ?? String(index),
    anchorTo: 'end',
    followOnAppend: 'auto',
    scrollEndThreshold: NEAR_BOTTOM_THRESHOLD,
    overscan: 6
  });

  /** Positioned rows currently rendered (the virtualization window) */
  const virtualRows = computed(() => virtualizer.value.getVirtualItems());
  /** Total scrollable height of all measured rows */
  const totalSize = computed(() => virtualizer.value.getTotalSize());

  /**
   * Row group by virtual row index (template helper: virtual rows carry the
   * index, the model carries the group).
   * @param index
   */
  const rowGroup = (index: number): MessageItem[] => rows.value[index]?.group ?? [];

  /** "Scroll to bottom" floating button visibility (hidden while pinned at the end) */
  const showScrollBottom = ref(false);

  /**
   * Sync the button visibility on scroll, measured from the LIVE DOM
   * (`scrollHeight - scrollTop - clientHeight`) rather than the virtualizer's
   * internal offset: the virtualizer's tracked offset lags a frame behind a
   * programmatic scroll, which left the button stale.
   */
  const updateScrollBottomBtn = () => {
    const el = scrollContainerRef.value;
    if (!el) return;
    showScrollBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight > NEAR_BOTTOM_THRESHOLD;
  };

  /** Snap the live DOM to the bottom (clamped to the current spacer height). */
  const snapToBottom = () => {
    const el = scrollContainerRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  };

  /**
   * Scroll the chat list to the end (making the newest message visible).
   *
   * Two passes: `scrollToEnd()` targets the virtualizer's CURRENT measured
   * total, but a growing last row (streaming) re-measures one frame later, so
   * the rAF pass re-snaps against the updated spacer — without it the tail
   * stays a growth-delta (tens of px) above the true bottom.
   */
  const scrollToBottom = () => {
    nextTick(() => {
      virtualizer.value.scrollToEnd();
      snapToBottom();
      requestAnimationFrame(() => {
        snapToBottom();
        updateScrollBottomBtn();
      });
    });
  };

  /**
   * Follow strategy on list changes: a newly appended USER message always
   * scrolls to the end; every other change (streaming chunks, tool events)
   * is left to `followOnAppend: 'auto'`, which follows only while pinned —
   * streaming must not yank the view down while the user reviews history.
   */
  watch(messages, (msgs, oldMsgs) => {
    const added = (msgs ?? []).slice(oldMsgs?.length ?? 0);
    // The watch flushes pre-DOM-update, so this reads the PRE-change geometry.
    const el = scrollContainerRef.value;
    const wasNearBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD;
    if (added.some(m => m.role === CHAT_ROLE.USER) || wasNearBottom) {
      scrollToBottom();
    }
  });

  // After the component mounts (first page open), scroll to the end so the latest messages are visible
  onMounted(() => scrollToBottom());

  return {
    scrollContainerRef,
    rows,
    virtualRows,
    virtualizer,
    totalSize,
    rowGroup,
    showScrollBottom,
    scrollToBottom,
    updateScrollBottomBtn
  };
}
