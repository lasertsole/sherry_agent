/**
 * Quoted-selection chips for the chat composer.
 *
 * A quote chip is the sibling of the file-mention chip: an atomic
 * `<span contenteditable="false">` the caret skips over, carrying the selected
 * text in `data-quote-text` while showing only a short preview. On send,
 * :func:`extractTextForSend` turns it into a markdown blockquote, so the model
 * can tell a quoted passage apart from the user's own words.
 *
 * @module composables/use-text-selection
 */

/** The chip class (an atomic `contenteditable=false` node inside the composer). */
export const QUOTE_CHIP_CLASS = 'text-quote-chip';

/** The attribute holding the full quoted text (what a send extracts). */
export const QUOTE_ATTR = 'data-quote-text';

/** Longest preview shown inside the chip (the full text stays in the attribute). */
const QUOTE_PREVIEW_CHARS = 40;

/**
 * Build the chip element for one quoted selection.
 * @param text The selected text (stored in full in the attribute).
 * @param title Tooltip text (the caller supplies a localized source label).
 * @returns The chip element.
 */
export function createQuoteChip(text: string, title?: string): HTMLElement {
  const span = document.createElement('span');
  span.className = QUOTE_CHIP_CLASS;
  span.contentEditable = 'false';
  span.setAttribute(QUOTE_ATTR, text);
  const preview = text.length > QUOTE_PREVIEW_CHARS ? `${text.slice(0, QUOTE_PREVIEW_CHARS)}…` : text;
  span.textContent = `💬 ${preview}`;
  if (title) span.title = title;
  return span;
}

/**
 * Insert a quote chip at the caret, followed by a space (the file-mention
 * insertion shape), then notify the host so its draft model syncs.
 *
 * Falls back to appending at the end when the caret is not inside the composer
 * (a right-click never moved it there).
 * @param inputEl The composer's contenteditable element.
 * @param text The selected text.
 * @param title Tooltip text for the chip.
 */
export function insertQuoteChip(inputEl: HTMLElement | null, text: string, title?: string): void {
  if (!inputEl) return;
  const chip = createQuoteChip(text, title);
  const space = document.createTextNode(' ');
  const selection = window.getSelection();
  const range =
    selection && selection.rangeCount > 0 && inputEl.contains(selection.getRangeAt(0).commonAncestorContainer)
      ? selection.getRangeAt(0)
      : null;
  if (range) {
    range.deleteContents();
    // insertNode leaves the range pointing BEFORE the node it just inserted,
    // so the space goes first and the chip lands ahead of it.
    range.insertNode(space);
    range.insertNode(chip);
    range.setStartAfter(space);
    range.collapse(true);
    selection?.removeAllRanges();
    selection?.addRange(range);
  } else {
    inputEl.appendChild(chip);
    inputEl.appendChild(space);
  }
  inputEl.focus();
  inputEl.dispatchEvent(new InputEvent('input', { bubbles: true }));
}

/**
 * The markdown blockquote a quote chip stands for: every line prefixed with
 * `> ` and the whole block surrounded by blank lines, so it reads as a quote
 * block in the sent message.
 * @param text The quoted text.
 * @returns The blockquote text (leading/trailing newlines included).
 */
export function buildQuoteBlock(text: string): string {
  const quoted = text
    .split(/\r?\n/)
    .map(line => `> ${line}`)
    .join('\n');
  return `\n${quoted}\n`;
}
