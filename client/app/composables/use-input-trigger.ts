/**
 * Composer trigger detection for `$skill` and `/command` references.
 *
 * The composer stays a plain contenteditable: this module only parses the text
 * before the caret, scores candidates and performs the small DOM operations the
 * panel needs. There is no editor framework.
 *
 * @module composables/use-input-trigger
 */

// Stable specifier so the pure module works under Vitest as well as in Nuxt.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { extractTextForSend } from './use-file-mention';

/** The two panel kinds this module opens (file `@` keeps its existing mentions). */
export type TriggerKind = 'skill' | 'slash';

/** One detected trigger immediately before the caret. */
export interface TriggerMatch {
  kind: TriggerKind;
  /** Raw text after the trigger character (what the panel filters on). */
  query: string;
  /** Number of DOM characters to remove when an item is chosen. */
  tokenLength: number;
}

const TRIGGER_RE = /(^|\s)([/$¥￥])([^\s/$¥￥]*)$/;

/**
 * Detect a trigger at the end of the text before the caret.
 * @param text Text before the caret (newlines included).
 * @param caretOffset Caret offset in that text.
 */
export function detectTrigger(text: string, caretOffset: number): TriggerMatch | null {
  const before = text.slice(0, Math.max(0, caretOffset));
  const match = before.match(TRIGGER_RE);
  if (!match) return null;
  const raw = match[2];
  const char = raw === '¥' || raw === '￥' ? '$' : raw;
  const kind: TriggerKind = char === '/' ? 'slash' : 'skill';
  const query = match[3] ?? '';
  return { kind, query, tokenLength: 1 + query.length };
}

/**
 * Score a candidate for the panel. Empty query matches everything; prefix beats
 * substring beats description/keyword hits. No subsequence matching — the same
 * coarse scale as the command palette.
 * @param label Primary label (skill/command name).
 * @param description Secondary text.
 * @param query Lowercased or raw query.
 */
export function scoreTriggerMatch(label: string, description: string, query: string): number {
  const q = String(query ?? '')
    .trim()
    .toLowerCase();
  if (!q) return 1;
  const name = String(label ?? '').toLowerCase();
  if (name === q) return 1000;
  if (name.startsWith(q)) return 500 - Math.min(name.length, 100);
  const at = name.indexOf(q);
  if (at >= 0) return 300 - Math.min(at, 100);
  const desc = String(description ?? '').toLowerCase();
  if (desc.startsWith(q)) return 120;
  if (desc.includes(q)) return 60;
  return -1;
}

/**
 * The plain text before the current caret inside *root*.
 * @param root The composer's contenteditable element.
 * @returns Text before the caret, or null when the caret is outside the composer.
 */
export function textBeforeCaret(root: HTMLElement | null): string | null {
  if (!root) return null;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.endContainer)) return null;
  try {
    const before = document.createRange();
    before.selectNodeContents(root);
    before.setEnd(range.endContainer, range.endOffset);
    const holder = document.createElement('div');
    holder.appendChild(before.cloneContents());
    return extractTextForSend(holder);
  } catch {
    return null;
  }
}

/**
 * Whether the caret currently sits inside a pasted code block.
 * @param root The composer's contenteditable element.
 */
export function isCaretInCode(root: HTMLElement | null): boolean {
  if (!root) return false;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return false;
  let node: Node | null = selection.getRangeAt(0).startContainer;
  while (node && node !== root) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = (node as Element).tagName;
      if (tag === 'PRE' || tag === 'CODE') return true;
    }
    node = node.parentNode;
  }
  return false;
}

/**
 * Delete the typed trigger token immediately before the caret.
 *
 * `Selection.modify` is what keeps this independent of the DOM's text-node
 * splits (a typed word can span several nodes); the `execCommand` fallback
 * covers an engine without it. A browser that supports neither leaves the
 * trigger text in place — the chip still inserts, only the literal `$query`
 * remains visible.
 * @param count Characters to delete (trigger character + query).
 */
export function removeTriggerChars(count: number): void {
  if (count <= 0) return;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return;
  const advanced = selection as Selection & {
    modify?: (alter: 'extend', direction: 'backward', granularity: 'character') => void;
  };
  if (typeof advanced.modify === 'function') {
    for (let index = 0; index < count; index += 1) {
      advanced.modify('extend', 'backward', 'character');
    }
    selection.deleteFromDocument();
    selection.collapseToEnd();
    return;
  }
  for (let index = 0; index < count; index += 1) {
    document.execCommand('delete');
  }
}

/**
 * Insert an atomic chip at the caret and leave a trailing space.
 * @param inputEl The composer's contenteditable element.
 * @param chip The chip element to insert.
 */
export function insertChipAtCaret(inputEl: HTMLElement | null, chip: HTMLElement): void {
  if (!inputEl) return;
  const space = document.createTextNode(' ');
  const selection = window.getSelection();
  const range =
    selection && selection.rangeCount > 0 && inputEl.contains(selection.getRangeAt(0).commonAncestorContainer)
      ? selection.getRangeAt(0)
      : null;
  if (range) {
    range.deleteContents();
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
 * Build the atomic skill chip for one skill name.
 * @param name Skill name.
 */
export function createSkillChip(name: string): HTMLElement {
  const chip = document.createElement('span');
  chip.className = 'skill-mention-chip';
  chip.contentEditable = 'false';
  chip.dataset.skill = name;
  chip.textContent = `$${name}`;
  chip.title = `$${name}`;
  return chip;
}

/**
 * Build the atomic command chip. The expanded prompt body rides in
 * `data-command-body`, which `extractTextForSend` uses instead of the visible
 * `/name` label.
 * @param name Command name.
 * @param body Expanded prompt template.
 */
export function createCommandChip(name: string, body: string): HTMLElement {
  const chip = document.createElement('span');
  chip.className = 'command-mention-chip';
  chip.contentEditable = 'false';
  chip.dataset.command = name;
  chip.dataset.commandBody = body;
  chip.textContent = `/${name}`;
  chip.title = `/${name}`;
  return chip;
}
