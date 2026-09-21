/**
 * Turn/group derivation for the chat message list (ChatBox).
 *
 * Pure view-model logic extracted from `ChatBox.vue` so the rendering component
 * only owns the template:
 *  - `filteredMessages` drops AI "empty placeholder" rows (no content and no
 *    reasoning) from the **render order**;
 *  - `consecutiveIdSet`/`isConsecutive` decide avatar hiding and compact bubble
 *    corners, computed on the **original (unfiltered)** sequence so an empty AI
 *    placeholder still acts as a turn boundary;
 *  - `turnGroups` groups rendered rows by turn (a USER row always starts its own
 *    group; consecutive AI/TOOL rows merge) to drive the segmented spacing;
 *  - `isBackgroundTask`/`regularMessages`/`backgroundCarriers` split
 *    subagent-completion carrier rows (USER rows with a non-user origin) out of
 *    the regular bubble flow.
 *
 * The observable output (message order, group boundaries, class decisions) is
 * identical to the previous in-component implementation.
 *
 * @module composables/use-chat-turn-groups
 */
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

/**
 * Create the turn-grouping view model for a message list.
 * @param messages Getter returning the current message list (the component's `props.messages`)
 */
export function useChatTurnGroups(messages: () => MessageItem[] | undefined) {
  const filteredMessages = computed<MessageItem[]>(() => {
    return (messages() ?? []).filter((item: MessageItem) => {
      // Hide "AI empty placeholder" messages: right after sending, when the AI has not produced
      // any content yet (no tool calls, no thinking content either), do not render this placeholder
      // bubble containing only a name + an empty box, so "Sherry" does not look glued to a white box.
      // But empty-body messages carrying reasoning must pass through: the thinking bubble uses them
      // as its host, otherwise the model thinking block would be filtered out together with the
      // empty placeholder and the thinking bubble could never render.
      if (item.role === CHAT_ROLE.AI && !item.content.trim() && !item.reasoning) {
        return false;
      }
      return true;
    });
  });

  /**
   * Determine whether a message should be rendered as a "consecutive message"
   * (no avatar shown, compact spacing, square-cornered bubbles touching each other).
   *
   * The check must be performed on the **original (unfiltered)** message sequence, not on
   * `filteredMessages`: filteredMessages drops "AI empty placeholder" messages, but an empty
   * placeholder is a real turn boundary (handleSend appends an empty AI placeholder after every
   * user message). If adjacency-by-same-role were judged on the filtered list, the AI placeholder
   * in [userA, AI empty placeholder, userB] would be removed, making userB be misjudged as a
   * consecutive message of the preceding userA —— exactly the root cause of "the first message
   * sent after opening the page was treated as a consecutive message".
   *
   * Correct semantics: skip TOOL rows in the original sequence and only check whether the nearest
   * preceding visible message has the same role. An empty AI placeholder still keeps the `ai` role,
   * which differs from the user role, so it naturally acts as a turn separator; multiple same-role
   * rows within one turn (e.g. tool calls + the final reply inside a single AI turn) are still
   * correctly judged as consecutive.
   */
  const consecutiveIdSet = computed<Set<number>>(() => {
    const result = new Set<number>();
    let prevRole: CHAT_ROLE | null = null;
    for (const item of messages() ?? []) {
      if (item.role === CHAT_ROLE.TOOL) {
        continue;
      }
      if (prevRole === item.role) {
        result.add(item.id);
      }
      prevRole = item.role;
    }
    return result;
  });

  const isConsecutive = (id: number): boolean => consecutiveIdSet.value.has(id);

  /**
   * Group the rendered messages by "turn" to implement segmented spacing:
   *  - User messages (USER) each form their own group: both neighbors are turn boundaries, with
   *    the outer gap-6 (24px) providing the separation;
   *  - Consecutive AI/TOOL rows after the first message are grouped together: the group is
   *    tightened with gap-3 (12px) (bubble↔tool card↔bubble compactly joined, including
   *    tool-call spans).
   *
   * Grouping is based on the **render order** (filteredMessages): empty AI placeholder messages
   * have already been filtered out, so `user → AI placeholder (filtered) → user` become directly
   * adjacent in render order; the second user row correctly starts a new turn.
   */
  const turnGroups = computed<MessageItem[][]>(() => {
    const groups: MessageItem[][] = [];
    for (const item of filteredMessages.value) {
      const last = groups.length ? groups[groups.length - 1] : null;
      const prevRole = last ? (last[last.length - 1]?.role ?? null) : null;
      // New turn: first message, this row is a user (always forms its own group), or the previous
      // row is a user (separating the AI reply from the user bubble)
      if (item.role === CHAT_ROLE.USER || groups.length === 0 || prevRole === CHAT_ROLE.USER) {
        groups.push([item]);
      } else {
        // Consecutive AI/TOOL rows → merge into the last non-user group
        groups[groups.length - 1]?.push(item);
      }
    }
    return groups;
  });

  /**
   * Determine whether a turn group should use the 12px inner spacing (flex gap-3).
   * Only groups that are "multi-row with a non-user first row" (pure AI/TOOL consecutive rows)
   * use gap-3; single-row groups or groups containing a user do not need it.
   * @param group
   */
  const turnSpacingClass = (group: MessageItem[]): boolean => group.length > 1 && group[0]?.role !== CHAT_ROLE.USER;

  /**
   * Background-task completion carrier: a USER-role message whose backend origin is
   * an internal source (e.g. "subagent_completion"). Carriers render as a centered, muted
   * system card instead of the regular user bubble; user-origin rows ("user") and legacy
   * rows without origin (TEXT NULL = a real user message) keep the user-bubble rendering.
   * @param message
   */
  const isBackgroundTask = (message: MessageItem): boolean =>
    message.role === CHAT_ROLE.USER && !!message.origin && message.origin !== 'user';

  /**
   * Messages of a turn group that render as regular rows (background-task carriers excluded).
   * @param group
   */
  const regularMessages = (group: MessageItem[]): MessageItem[] => group.filter(m => !isBackgroundTask(m));

  /**
   * Background-task carriers of a turn group (USER rows always form singleton groups).
   * @param group
   */
  const backgroundCarriers = (group: MessageItem[]): MessageItem[] => group.filter(isBackgroundTask);

  return {
    filteredMessages,
    isConsecutive,
    turnGroups,
    turnSpacingClass,
    isBackgroundTask,
    regularMessages,
    backgroundCarriers
  };
}
