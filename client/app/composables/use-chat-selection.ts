/**
 * Chat-area text selection: the payload the right-click menu acts on.
 *
 * The guard is deliberately narrow — the selection must live inside ONE
 * message row, must not start in an interactive control or an approval card,
 * and must fit the quote budget. A selection that fails any check returns
 * `null`, which lets the native context menu appear untouched.
 *
 * @module composables/use-chat-selection
 */

/** One quoted selection and where it came from. */
export interface SelectionPayload {
  /** The selected plain text (trimmed). */
  text: string;
  /** The source message's id (`data-message-id`), when it has one. */
  sourceMessageId?: string;
  /** The source row's role (`user` / `ai` / `tool`). */
  sourceRole?: string;
}

/** A single quote may not exceed this many characters. */
export const MAX_SELECTION_CHARS = 8_000;

/** Controls whose text must never become a quote (a button label is not prose). */
const EXCLUDED_SELECTORS = [
  'button',
  'input',
  'textarea',
  'select',
  '[contenteditable="true"]',
  '[role="dialog"]',
  '[role="menu"]',
  '[data-hitl-card]',
  '.p-contextmenu'
].join(', ');

/**
 * Read `window.getSelection()` and turn it into a quote payload.
 *
 * @param root The chat list container (the selection must live inside it).
 * @returns The payload, or `null` when there is nothing valid to quote.
 */
export function inspectSelection(root?: Element | null): SelectionPayload | null {
  if (typeof window === 'undefined') return null;
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;

  const text = selection.toString().trim();
  if (!text || text.length > MAX_SELECTION_CHARS) return null;

  const range = selection.getRangeAt(0);
  const container = range.commonAncestorContainer;
  if (root && !root.contains(container)) return null;

  const element =
    container.nodeType === Node.ELEMENT_NODE ? (container as Element) : (container.parentElement as Element | null);
  if (!element) return null;
  if (element.closest(EXCLUDED_SELECTORS)) return null;

  // One message per quote: the nearest row is what the payload names. A
  // selection spanning two rows has the group wrapper as its common ancestor,
  // which carries no message id, so it is refused.
  const row = element.closest('[data-message-id]');
  if (!row) return null;

  return {
    text,
    sourceMessageId: row.getAttribute('data-message-id') || undefined,
    sourceRole: row.getAttribute('data-message-role') || undefined
  };
}
