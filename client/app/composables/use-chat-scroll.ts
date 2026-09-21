/**
 * Scroll management for the chat message list (ChatBox).
 *
 * Extracted from `ChatBox.vue` with identical observable behavior:
 *  - the scroll container template ref (`scrollContainerRef`, bound by name in
 *    the component template);
 *  - the "scroll to bottom" floating button visibility, driven by the 80px
 *    NEAR_BOTTOM_THRESHOLD;
 *  - the after-change scrolling strategy: always follow a newly appended USER
 *    message, otherwise only follow while the user is already near the bottom
 *    (streaming output must not yank the view down while the user reviews
 *    history).
 *
 * @module composables/use-chat-scroll
 */
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

/** "Near bottom" threshold (px): a distance to the bottom within this value means the user is still following the latest messages */
const NEAR_BOTTOM_THRESHOLD = 80;

/**
 * Create the scroll controller for the chat list.
 * @param messages Getter returning the current message list (the component's `props.messages`)
 */
export function useChatScroll(messages: () => MessageItem[] | undefined) {
  /** Chat list scroll container (the outermost overflow-auto div), used for auto-scrolling to the bottom */
  const scrollContainerRef = useTemplateRef<HTMLDivElement>('scrollContainerRef');

  /**
   * Determine whether the user is currently near the bottom of the list.
   *
   * Measured before the DOM update (watch defaults to pre flush), so what is read is the scroll
   * state before this change has rendered.
   */
  const isNearBottom = (): boolean => {
    const el = scrollContainerRef.value;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD;
  };

  /** "Scroll to bottom" floating button visibility: shown when the scroll position is more than NEAR_BOTTOM_THRESHOLD (80px) from the bottom */
  const showScrollBottom = ref(false);

  /**
   * Sync the "scroll to bottom" button visibility on scroll (triggered by the scroll container's
   * @scroll). The programmatic scroll in scrollToBottom also dispatches a scroll event, so the
   * button hides accordingly.
   */
  const updateScrollBottomBtn = () => {
    const el = scrollContainerRef.value;
    if (!el) return;
    showScrollBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight > NEAR_BOTTOM_THRESHOLD;
  };

  /**
   * Scroll the chat list to the bottom (making new messages visible).
   *
   * scrollHeight must be read after the DOM update (nextTick); otherwise the measured value is the
   * old height and the scroll cannot reach the bottom of the newest messages. The parent component
   * reassigns the messages array (new reference) every time a streaming chunk arrives, so watching
   * the reference change covers all three scenarios: "first page load", "sending a message", and
   * "each AI reply chunk".
   */
  const scrollToBottom = () => {
    nextTick(() => {
      const el = scrollContainerRef.value;
      if (el) {
        el.scrollTop = el.scrollHeight;
        updateScrollBottomBtn();
      }
    });
  };

  /**
   * Scrolling strategy after the message list changes:
   * - New user messages added (sending a message / loading a history session): always scroll to
   *   the bottom;
   * - Other changes (AI streaming chunk-by-chunk appends, tool events, etc.): follow only while
   *   the user is still near the bottom, preventing streaming output from yanking the user back
   *   down while they scroll up to review history.
   */
  watch(messages, (msgs, oldMsgs) => {
    const added = (msgs ?? []).slice(oldMsgs?.length ?? 0);
    if (added.some(m => m.role === CHAT_ROLE.USER) || isNearBottom()) {
      scrollToBottom();
    }
  });

  // After the component mounts (first page open), scroll to the bottom so the latest messages are visible
  onMounted(() => scrollToBottom());

  return { scrollContainerRef, showScrollBottom, scrollToBottom, updateScrollBottomBtn };
}
