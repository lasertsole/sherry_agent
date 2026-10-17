/**
 * File mentions: the `@path` chips the chat composer carries as context.
 *
 * Three entries converge on :func:`insertFileMention` — the file tree's
 * right-click "Add to chat", a workspace file dragged onto the composer, and a
 * file-viewer tab dragged there — and all three send the same thing: a PATH
 * reference, never the file's content (the agent has `read_file` and decides
 * itself whether to read it).
 *
 * The chip is an atomic `<span contenteditable="false">` inside the composer's
 * contenteditable div, showing an icon + the file name; the text it STANDS FOR
 * (`@./src/foo.py`) lives in its `data-file-path`, and
 * :func:`extractTextForSend` splices that back in when the message is sent.
 *
 * @module composables/use-file-mention
 */

/** A workspace file/directory reference carried by a chip. */
export interface FileMentionPayload {
  /** Which kind of entry the reference points at (directories get a trailing `/`). */
  type: 'file' | 'dir';
  /** The path relative to the session's project root. */
  path: string;
  /** The entry's own name (what the chip shows). */
  name: string;
}

/** The drag MIME carrying a :type:`FileMentionPayload` as JSON. */
export const FILE_MENTION_MIME = 'text/x-sherry-file';

/** The chip class (`contenteditable=false` atoms inside the composer). */
export const FILE_MENTION_CLASS = 'file-mention-chip';

/**
 * The mention text a payload stands for: `@./relative/path` (a directory keeps
 * its trailing slash). The `./` prefix keeps the reference unambiguous for the
 * model — it reads as a relative path, not as a bare word.
 * @param payload The referenced entry.
 */
export function buildMentionText(payload: FileMentionPayload): string {
  const path = payload.type === 'dir' && !payload.path.endsWith('/') ? `${payload.path}/` : payload.path;
  return `@${path.startsWith('./') || path.startsWith('/') || path.startsWith('.') ? path : `./${path}`}`;
}

/**
 * An icon for the chip, mirroring the file tree's glyphs.
 * @param payload
 */
function iconFor(payload: FileMentionPayload): string {
  if (payload.type === 'dir') return '📁';
  const lower = payload.name.toLowerCase();
  if (lower.endsWith('.md')) return '📝';
  if (lower.endsWith('.py') || lower.endsWith('.ts') || lower.endsWith('.js')) return '📜';
  return '📄';
}

/**
 * Build the chip element for one payload.
 * @param payload The referenced entry.
 */
export function createMentionChip(payload: FileMentionPayload): HTMLElement {
  const span = document.createElement('span');
  span.className = FILE_MENTION_CLASS;
  span.contentEditable = 'false';
  span.dataset.filePath = buildMentionText(payload);
  span.dataset.fileType = payload.type;
  span.title = payload.path;
  span.textContent = `${iconFor(payload)} ${payload.name}`;
  return span;
}

/**
 * Insert a mention chip at the caret, followed by a space (the same shape
 * ZCode's composer produces), then notify the host so its draft model syncs.
 *
 * Falls back to appending at the end when the caret is not inside the composer
 * (a right-click never moved it there).
 * @param inputEl The composer's contenteditable element.
 * @param payload The referenced entry.
 */
export function insertFileMention(inputEl: HTMLElement | null, payload: FileMentionPayload): void {
  if (!inputEl) return;
  const chip = createMentionChip(payload);
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
    // Leave the caret AFTER the trailing space so the next keystroke starts a new word.
    range.setStartAfter(space);
    range.collapse(true);
    selection?.removeAllRanges();
    selection?.addRange(range);
  } else {
    inputEl.appendChild(chip);
    inputEl.appendChild(space);
  }
  inputEl.focus();
  // The host's `@input` listener reads `textContent`, which already includes the
  // chip's label — the event is what keeps its draft model in step.
  inputEl.dispatchEvent(new InputEvent('input', { bubbles: true }));
}

/**
 * The plain text a composer's DOM stands for: text nodes as they are, mention
 * chips replaced by the `@path` they carry, line breaks as `\n`.
 *
 * This is what gets SENT — the chip's visible label (icon + file name) never
 * reaches the model.
 * @param inputEl The composer's contenteditable element.
 */
export function extractTextForSend(inputEl: HTMLElement | null): string {
  if (!inputEl) return '';
  let result = '';
  for (const node of Array.from(inputEl.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) {
      result += node.textContent ?? '';
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) continue;
    const el = node as HTMLElement;
    if (el.classList.contains(FILE_MENTION_CLASS)) {
      result += el.dataset.filePath ?? el.textContent ?? '';
      continue;
    }
    if (el.tagName === 'BR') {
      result += '\n';
      continue;
    }
    // Any other element (a nested block, a pasted fragment): recurse, so its own
    // chips still resolve to their paths.
    result += extractTextForSend(el);
  }
  return result;
}

/**
 * Request that a file mention be inserted into a session's composer.
 *
 * The file tree and the sidebar tabs live outside the chat page (left sidebar,
 * right sidebar), so the request travels over the app-wide mitt bus and the
 * page whose session it names performs the insertion — the same hop the
 * existing cross-shell events use.
 * @param sessionId The session whose composer should receive the mention.
 * @param payload The referenced entry.
 */
export function requestFileMention(sessionId: string, payload: FileMentionPayload): void {
  emit('chat:file-mention', { sessionId, payload });
}
