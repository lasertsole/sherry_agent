/**
 * The right-click selection guard: one message row, no controls, within budget.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { inspectSelection, MAX_SELECTION_CHARS } from '../use-chat-selection';

afterEach(() => {
  window.getSelection()?.removeAllRanges();
});

/**
 * Build a chat list with a message row and return its selectable text node.
 * @param role
 */
function buildRow(role = 'ai'): { root: HTMLElement; row: HTMLElement; text: Text } {
  const root = document.createElement('div');
  const row = document.createElement('div');
  row.dataset.messageId = '7';
  row.dataset.messageRole = role;
  const paragraph = document.createElement('p');
  const text = document.createTextNode('quoted answer');
  paragraph.appendChild(text);
  row.appendChild(paragraph);
  root.appendChild(row);
  document.body.appendChild(root);
  return { root, row, text };
}

/**
 * Select a range inside the given node.
 * @param node
 * @param start
 * @param end
 */
function select(node: Node, start = 0, end = Number.MAX_SAFE_INTEGER): void {
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, Math.min(end, node.textContent?.length ?? 0));
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
}

describe('inspectSelection', () => {
  it('returns the text and the source row', () => {
    const { root, text } = buildRow('user');
    select(text);

    expect(inspectSelection(root)).toEqual({
      text: 'quoted answer',
      sourceMessageId: '7',
      sourceRole: 'user'
    });
  });

  it('refuses a selection outside the chat list', () => {
    const { text } = buildRow();
    select(text);

    expect(inspectSelection(document.createElement('div'))).toBeNull();
  });

  it('refuses a selection inside a button', () => {
    const { root, row } = buildRow();
    const button = document.createElement('button');
    button.textContent = 'Copy';
    row.appendChild(button);
    const text = button.firstChild as Text;
    select(text);

    expect(inspectSelection(root)).toBeNull();
  });

  it('refuses a selection that spans two message rows', () => {
    const { root, row } = buildRow();
    const second = document.createElement('div');
    second.dataset.messageId = '8';
    second.dataset.messageRole = 'ai';
    second.textContent = 'second row';
    root.appendChild(second);
    const range = document.createRange();
    range.setStart(row.firstChild!.firstChild!, 0);
    range.setEnd(second.firstChild!, 3);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);

    expect(inspectSelection(root)).toBeNull();
  });

  it('refuses a selection over the quote budget', () => {
    const { root, row } = buildRow();
    row.textContent = 'x'.repeat(MAX_SELECTION_CHARS + 1);
    select(row.firstChild as Text);

    expect(inspectSelection(root)).toBeNull();
  });

  it('refuses a collapsed selection', () => {
    const { root, text } = buildRow();
    select(text, 2, 2);

    expect(inspectSelection(root)).toBeNull();
  });
});
