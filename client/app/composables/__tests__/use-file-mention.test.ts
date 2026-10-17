/**
 * File mentions: the `@path` chips the composer carries.
 *
 * Contract: a mention renders as an atomic chip whose visible label is the
 * icon + file name, while the text it STANDS FOR (`@./relative/path`) lives in
 * `data-file-path` and is what a send extracts — the label must never reach
 * the model. Directories keep a trailing slash.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  FILE_MENTION_CLASS,
  FILE_MENTION_MIME,
  buildMentionText,
  createMentionChip,
  extractTextForSend,
  insertFileMention,
  requestFileMention,
  type FileMentionPayload
} from '../use-file-mention';

const file: FileMentionPayload = { type: 'file', path: 'src/foo.py', name: 'foo.py' };
const dir: FileMentionPayload = { type: 'dir', path: 'src', name: 'src' };

describe('buildMentionText', () => {
  it('prefixes a relative file path with ./', () => {
    expect(buildMentionText(file)).toBe('@./src/foo.py');
  });

  it('keeps a trailing slash on directories', () => {
    expect(buildMentionText(dir)).toBe('@./src/');
  });

  it('does not double the ./ prefix', () => {
    expect(buildMentionText({ type: 'file', path: './already.py', name: 'already.py' })).toBe('@./already.py');
  });

  it('leaves an existing trailing slash alone', () => {
    expect(buildMentionText({ type: 'dir', path: 'src/', name: 'src' })).toBe('@./src/');
  });
});

describe('createMentionChip', () => {
  it('is an atomic, non-editable chip carrying the mention text', () => {
    const chip = createMentionChip(file);
    expect(chip.className).toBe(FILE_MENTION_CLASS);
    expect(chip.contentEditable).toBe('false');
    expect(chip.dataset.filePath).toBe('@./src/foo.py');
    expect(chip.dataset.fileType).toBe('file');
    expect(chip.textContent).toContain('foo.py');
  });

  it('marks directories as such', () => {
    expect(createMentionChip(dir).dataset.fileType).toBe('dir');
  });
});

describe('extractTextForSend', () => {
  it('resolves a chip to its @path, not its label', () => {
    const host = document.createElement('div');
    host.appendChild(document.createTextNode('check '));
    host.appendChild(createMentionChip(file));
    host.appendChild(document.createTextNode(' please'));

    expect(extractTextForSend(host)).toBe('check @./src/foo.py please');
  });

  it('keeps plain text untouched', () => {
    const host = document.createElement('div');
    host.textContent = 'just text';
    expect(extractTextForSend(host)).toBe('just text');
  });

  it('turns a <br> into a newline', () => {
    const host = document.createElement('div');
    host.append(document.createTextNode('line1'), document.createElement('br'), document.createTextNode('line2'));
    expect(extractTextForSend(host)).toBe('line1\nline2');
  });

  it('recurses into nested elements so their chips resolve too', () => {
    const host = document.createElement('div');
    const inner = document.createElement('span');
    inner.appendChild(createMentionChip(dir));
    host.appendChild(inner);
    expect(extractTextForSend(host)).toBe('@./src/');
  });

  it('handles a null element', () => {
    expect(extractTextForSend(null)).toBe('');
  });
});

describe('insertFileMention', () => {
  let host: HTMLElement;

  beforeEach(() => {
    document.body.innerHTML = '';
    host = document.createElement('div');
    host.contentEditable = 'true';
    document.body.appendChild(host);
  });

  it('appends the chip + a space when no caret is set, and notifies the host', () => {
    const inputEvents: Event[] = [];
    host.addEventListener('input', event => inputEvents.push(event));

    insertFileMention(host, file);

    expect(host.querySelectorAll(`.${FILE_MENTION_CLASS}`)).toHaveLength(1);
    expect(host.textContent).toBe('📜 foo.py ');
    expect(inputEvents).toHaveLength(1);
  });

  it('splices the chip at the caret', () => {
    host.textContent = 'ab';
    const text = host.firstChild as Text;
    const range = document.createRange();
    range.setStart(text, 1);
    range.collapse(true);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);

    insertFileMention(host, dir);

    expect(extractTextForSend(host)).toBe('a@./src/ b');
  });

  it('is a no-op for a missing element', () => {
    expect(() => insertFileMention(null, file)).not.toThrow();
  });
});

describe('requestFileMention', () => {
  it('broadcasts to the composer over the app bus', async () => {
    const mitt = await import('../mitt');
    const spy = vi.spyOn(mitt, 'emit');
    requestFileMention('sid-1', file);
    expect(spy).toHaveBeenCalledWith('chat:file-mention', { sessionId: 'sid-1', payload: file });
    spy.mockRestore();
  });

  it('exports the drag MIME the drop handlers share', () => {
    expect(FILE_MENTION_MIME).toBe('text/x-sherry-file');
  });
});
