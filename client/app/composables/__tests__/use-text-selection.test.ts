/**
 * Quoted-selection chips: the composer atom and the blockquote it sends.
 */
import { describe, it, expect } from 'vitest';
import { QUOTE_ATTR, QUOTE_CHIP_CLASS, buildQuoteBlock, createQuoteChip, insertQuoteChip } from '../use-text-selection';
import { extractTextForSend } from '../use-file-mention';

describe('createQuoteChip', () => {
  it('is an atomic chip carrying the full text and a short preview', () => {
    const text = 'x'.repeat(80);
    const chip = createQuoteChip(text, '来自：助手回复');

    expect(chip.className).toBe(QUOTE_CHIP_CLASS);
    expect(chip.contentEditable).toBe('false');
    expect(chip.getAttribute(QUOTE_ATTR)).toBe(text);
    expect(chip.title).toBe('来自：助手回复');
    expect(chip.textContent).toContain('…');
    expect(chip.textContent!.length).toBeLessThan(text.length);
  });

  it('shows short text verbatim', () => {
    const chip = createQuoteChip('short quote');
    expect(chip.textContent).toBe('💬 short quote');
    expect(chip.title).toBe('');
  });
});

describe('buildQuoteBlock', () => {
  it('prefixes every line with > and surrounds the block with newlines', () => {
    expect(buildQuoteBlock('one\ntwo')).toBe('\n> one\n> two\n');
  });
});

describe('insertQuoteChip', () => {
  it('appends the chip and a trailing space when the caret is outside', () => {
    const host = document.createElement('div');
    host.contentEditable = 'true';

    insertQuoteChip(host, 'quoted');

    expect(host.querySelector(`.${QUOTE_CHIP_CLASS}`)).not.toBeNull();
    expect(host.lastChild?.nodeType).toBe(Node.TEXT_NODE);
    expect(host.textContent).toContain('quoted');
  });
});

describe('extractTextForSend with a quote chip', () => {
  it('sends the blockquote, never the chip preview', () => {
    const host = document.createElement('div');
    host.appendChild(document.createTextNode('question: '));
    host.appendChild(createQuoteChip('line one\nline two'));
    host.appendChild(document.createTextNode(' more'));

    expect(extractTextForSend(host)).toBe('question: \n> line one\n> line two\n more');
  });

  it('leaves file-mention extraction unchanged beside a quote', () => {
    const host = document.createElement('div');
    const fileChip = document.createElement('span');
    fileChip.className = 'file-mention-chip';
    fileChip.dataset.filePath = '@./src/foo.py';
    fileChip.textContent = '📄 foo.py';
    host.appendChild(fileChip);
    host.appendChild(createQuoteChip('quoted'));

    expect(extractTextForSend(host)).toBe('@./src/foo.py\n> quoted\n');
  });
});
