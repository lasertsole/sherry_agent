/**
 * Expand/collapse state for chat card bodies (ChatBox).
 *
 * Extracted from `ChatBox.vue`: three per-message id sets (collapsed by
 * default) driving the tool-call card, the model-thinking block and the
 * injected-row card (background-task completion / system message). Kept as
 * shared sets owned by the ChatBox instance (not per-card local state) so a
 * given message id keeps its expansion state across re-renders, exactly as
 * before.
 *
 * @module composables/use-chat-card-expansion
 */
export function useChatCardExpansion() {
  /** Set of tool-card message ids currently expanded (collapsed by default) */
  const expandedToolCards = reactive(new Set<number>());

  /** Set of thinking-block message ids currently expanded (collapsed by default) */
  const expandedThinking = reactive(new Set<number>());

  /**
   * Set of injected-row card ids currently expanded (collapsed by default).
   * The background-task completion carrier and the generic system-message card
   * share it — they are one card, labelled by the row's origin.
   */
  const expandedCarriers = reactive(new Set<number>());

  /**
   * Toggle the expand/collapse state of a tool card
   * @param id
   */
  const toggleToolCard = (id: number) => {
    if (expandedToolCards.has(id)) {
      expandedToolCards.delete(id);
    } else {
      expandedToolCards.add(id);
    }
  };

  /**
   * Toggle the expand/collapse state of a message's thinking block
   * @param id
   */
  const toggleThinking = (id: number) => {
    if (expandedThinking.has(id)) {
      expandedThinking.delete(id);
    } else {
      expandedThinking.add(id);
    }
  };

  /**
   * Toggle the expand/collapse state of an injected-row card
   * @param id
   */
  const toggleCarrier = (id: number) => {
    if (expandedCarriers.has(id)) {
      expandedCarriers.delete(id);
    } else {
      expandedCarriers.add(id);
    }
  };

  return {
    expandedToolCards,
    expandedThinking,
    expandedCarriers,
    toggleToolCard,
    toggleThinking,
    toggleCarrier
  };
}
