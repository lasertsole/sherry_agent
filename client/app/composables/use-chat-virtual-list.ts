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
import { onActivated, onDeactivated } from 'vue';
import { useVirtualizer } from '@tanstack/vue-virtual';
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

/** "Near bottom" threshold (px): within this distance the user follows the latest messages */
export const NEAR_BOTTOM_THRESHOLD = 80;

/** First-render height guess per turn group; corrected by real measurement */
const GROUP_ESTIMATE_PX = 160;

/** Distance from the top (px) that triggers the older-history request */
const TOP_TRIGGER_PX = 120;

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

export interface ChatVirtualListOptions {
  /** Called when the viewport crosses the top trigger (re-arms after leaving). */
  onReachTop?: () => void;
}

/**
 * Create the virtual list controller for the chat message list.
 * @param groups Getter returning the turn groups in render order.
 * @param messages Getter returning the raw message list (apps detect user appends).
 * @param options Optional top-reach hook for scroll-up history pagination.
 */
export function useChatVirtualList(
  groups: () => MessageItem[][],
  messages: () => MessageItem[] | undefined,
  options: ChatVirtualListOptions = {}
) {
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
  // Continuously-tracked pin state / offset (consumed by the KeepAlive hooks
  // below): reading them at deactivation time is unreliable because the
  // container may already be detached — 0/0/0 metrics read as "pinned".
  let pinnedRecently = true;
  let ongoingScrollTop = 0;

  const updateScrollBottomBtn = () => {
    const el = scrollContainerRef.value;
    if (!el) return;
    const away = el.scrollHeight - el.scrollTop - el.clientHeight;
    showScrollBottom.value = away > NEAR_BOTTOM_THRESHOLD;
    if (el.scrollHeight > el.clientHeight + 1) {
      pinnedRecently = away <= NEAR_BOTTOM_THRESHOLD;
      ongoingScrollTop = el.scrollTop;
    }
  };

  /** Re-arm latch: one top-reach callback per crossing, not per scroll event. */
  let topArmed = true;

  /**
   * Container scroll handler: keeps the bottom button in sync and fires the
   * top-reach hook once per crossing (pulling up loads older history).
   */
  const onScroll = () => {
    updateScrollBottomBtn();
    if (!options.onReachTop) return;
    const el = scrollContainerRef.value;
    if (!el) return;
    if (el.scrollTop <= TOP_TRIGGER_PX) {
      if (topArmed) {
        topArmed = false;
        options.onReachTop();
      }
    } else {
      // Re-arm as soon as the viewport leaves the trigger zone: the prepend
      // compensation below lands just above TOP_TRIGGER_PX, so a continued
      // pull-up must be able to fetch the next page.
      topArmed = true;
    }
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
      const el = scrollContainerRef.value;
      if (!el) return;
      virtualizer.value.scrollToEnd();
      snapToBottom();
      // Keep re-pinning until the spacer height stops changing: rows that were
      // never rendered before (a fresh session, a KeepAlive reactivation) only
      // get their REAL heights measured a few frames after they enter the
      // window, and each growth would otherwise leave the tail just above the
      // true bottom.
      let last = el.scrollHeight;
      let attempts = 0;
      const settle = () => {
        virtualizer.value.scrollToEnd();
        const height = el.scrollHeight;
        if (height !== last && attempts < 8) {
          last = height;
          attempts += 1;
          requestAnimationFrame(settle);
          return;
        }
        snapToBottom();
        updateScrollBottomBtn();
      };
      requestAnimationFrame(settle);
    });
  };

  /**
   * Reading-position stability across prepends.
   *
   * `anchorTo: 'end'` pins the BOTTOM edge: when older history is prepended,
   * the content under the viewport would slide down by the prepended height.
   * A key change on the FIRST row means exactly that (appends never touch
   * row 0), so compensate the scroll offset once the new rows are laid out —
   * the same "keep what you were reading in place" rule chat clients use.
   */
  watch(
    () => rows.value[0]?.key,
    (newKey, oldKey) => {
      if (!oldKey || !newKey || oldKey === newKey) return;
      const el = scrollContainerRef.value;
      if (!el) return;
      const heightBefore = el.scrollHeight;
      nextTick(() => {
        // The prepended rows are measured by the ResizeObserver a few frames
        // after they render, so wait for the spacer height to settle before
        // applying the delta — a single rAF lands too early and reads the
        // still-unchanged height.
        let last = el.scrollHeight;
        let attempts = 0;
        const settle = () => {
          const height = el.scrollHeight;
          if (height !== last && attempts < 6) {
            last = height;
            attempts += 1;
            requestAnimationFrame(settle);
            return;
          }
          const delta = height - heightBefore;
          if (delta > 0) el.scrollTop += delta;
          updateScrollBottomBtn();
        };
        requestAnimationFrame(settle);
      });
    }
  );

  /**
   * Follow strategy on list changes: a newly appended USER message always
   * scrolls to the end; every other change (streaming chunks, tool events)
   * is left to `followOnAppend: 'auto'`, which follows only while pinned —
   * streaming must not yank the view down while the user reviews history.
   */
  watch(messages, (msgs, oldMsgs) => {
    if (restoringPosition) return; // an activation restore owns the offset
    // Judge growth by IDENTITY, not by array-length slicing: a history reload
    // merges cached older rows into the HEAD, which right-shifts every
    // existing row — a length slice would read the shifted tail as "new
    // messages" and yank a reader up the history to the bottom.
    const prevIds = new Set((oldMsgs ?? []).map(m => m.id));
    const added = (msgs ?? []).filter(m => !prevIds.has(m.id));
    const prevTailId = oldMsgs?.length ? oldMsgs[oldMsgs.length - 1]!.id : null;
    const newTailId = msgs?.length ? msgs[msgs.length - 1]!.id : null;
    // Only a genuine TAIL append (a send, including the very first message of
    // an empty session) may force a follow; a head prepend keeps the reader
    // where they are (the prepend compensation handles the viewport there).
    const appendedAtTail = newTailId !== null && prevTailId !== newTailId;
    // The watch flushes pre-DOM-update, so this reads the PRE-change geometry.
    const el = scrollContainerRef.value;
    // A degenerate viewport (content not measured yet, e.g. just re-attached
    // by KeepAlive) must not read as "near the bottom" and force a follow.
    const measured = !!el && el.scrollHeight > el.clientHeight + 1;
    const wasNearBottom =
      !el || (measured && el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD);
    if ((appendedAtTail && added.some(m => m.role === CHAT_ROLE.USER)) || wasNearBottom) {
      scrollToBottom();
    }
  });

  // After the component mounts (first page open), scroll to the end so the latest messages are visible
  onMounted(() => scrollToBottom());

  /**
   * KeepAlive lifecycle: pages are cached per session, and detaching the DOM
   * zeroes the container's scrollTop. For a virtual list the height lives in
   * the virtualizer, so nothing can recover the position on re-show — the
   * list would look empty until the first scroll event re-measures it.
   *
   * Deactivation records the PIN STATE (near the bottom or reading history):
   * a pinned list re-pins to the newest messages on activation (the chat
   * convention, and exact regardless of re-measurement), while a reader deep
   * in history keeps their pixel offset, re-applied until the re-measure
   * settles. Outside KeepAlive both hooks are no-ops.
   */
  /** While true, the follow watch must not fight the activation restore. */
  let restoringPosition = false;
  // No deactivation capture is needed: `pinnedRecently` / `ongoingScrollTop`
  // are maintained by every scroll event while the list is visible.
  onDeactivated(() => {});
  onActivated(() => {
    nextTick(() => {
      const el = scrollContainerRef.value;
      if (!el) return;
      virtualizer.value.measure();
      if (pinnedRecently) {
        scrollToBottom();
        return;
      }
      const lastScrollTop = ongoingScrollTop;
      restoringPosition = true;
      if (lastScrollTop > 0) el.scrollTop = lastScrollTop;
      updateScrollBottomBtn();
      // Keep re-applying the offset until the re-measure settles: rows that
      // were never rendered before activation get their real heights a few
      // frames in, and each growth shifts what sits under the viewport.
      let last = el.scrollHeight;
      let attempts = 0;
      const settle = () => {
        const height = el.scrollHeight;
        if (height !== last && attempts < 8) {
          last = height;
          attempts += 1;
          if (lastScrollTop > 0) el.scrollTop = lastScrollTop;
          requestAnimationFrame(settle);
          return;
        }
        if (lastScrollTop > 0) el.scrollTop = lastScrollTop;
        updateScrollBottomBtn();
        restoringPosition = false;
      };
      requestAnimationFrame(settle);
    });
  });

  return {
    scrollContainerRef,
    rows,
    virtualRows,
    virtualizer,
    totalSize,
    rowGroup,
    showScrollBottom,
    scrollToBottom,
    updateScrollBottomBtn,
    onScroll
  };
}
