import { describe, expect, it } from 'vitest';
import {
  allAddedDiff,
  parsePatchFileResult,
  parseToolDiff,
  parseUnifiedDiff,
  parseWriteFileResult,
  writeReportedNewFile
} from '../tool-diff';

/**
 * The parser is pinned to difflib's EXACT output: keepends lines joined with
 * newlines, `--- / +++` file headers, `@@ -l,c +l,c @@` hunks whose counts are
 * omitted when 1, and blank added/removed lines as length-1 rows.
 */

/** A real difflib payload (3-line context) for old→new. */
const SAMPLE_DIFF = [
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,6 +1,7 @@',
  ' import os',
  ' ',
  ' def main():',
  '-    print("old")',
  '+    print("new")',
  '+    print("extra")',
  ' ',
  ' if __name__ == "__main__":',
  '     main()'
].join('\n');

describe('parseUnifiedDiff', () => {
  it('classifies lines, strips prefixes, and counts +/-', () => {
    const diff = parseUnifiedDiff(SAMPLE_DIFF)!;

    expect(diff.addedCount).toBe(2);
    expect(diff.removedCount).toBe(1);
    const types = diff.lines.map(line => line.type);
    expect(types).toEqual([
      'hunk',
      'context',
      'context',
      'context',
      'removed',
      'added',
      'added',
      'context',
      'context',
      'context'
    ]);
    expect(diff.lines[4]).toMatchObject({ type: 'removed', content: '    print("old")' });
    expect(diff.lines[5]).toMatchObject({ type: 'added', content: '    print("new")' });
  });

  it('drops the file headers and keeps hunk rows with start numbers', () => {
    const diff = parseUnifiedDiff(SAMPLE_DIFF)!;

    expect(diff.lines.some(line => line.content.startsWith('---'))).toBe(false);
    const hunk = diff.lines[0];
    expect(hunk.type).toBe('hunk');
    expect(hunk.oldLineNo).toBe(1);
    expect(hunk.newLineNo).toBe(1);
  });

  it('advances line numbers per kind (old column skips additions)', () => {
    const diff = parseUnifiedDiff(SAMPLE_DIFF)!;

    // Hunk `@@ -1,6 +1,7 @@`: rows 1-3 are context at (1,1)..(3,3); the
    // removal consumes OLD 4 only; the additions land on NEW 4,5; the
    // trailing context walks (5,6) and (6,7).
    expect(diff.lines[1]).toMatchObject({ oldLineNo: 1, newLineNo: 1 });
    expect(diff.lines[3]).toMatchObject({ oldLineNo: 3, newLineNo: 3 });
    expect(diff.lines[4]).toMatchObject({ type: 'removed', oldLineNo: 4 });
    expect(diff.lines[5]).toMatchObject({ type: 'added', newLineNo: 4 });
    expect(diff.lines[6]).toMatchObject({ type: 'added', newLineNo: 5 });
    expect(diff.lines[7]).toMatchObject({ type: 'context', oldLineNo: 5, newLineNo: 6 });
    expect(diff.lines[9]).toMatchObject({ type: 'context', oldLineNo: 7, newLineNo: 8 });
  });

  it('parses the short hunk header (counts omitted when 1)', () => {
    const diff = parseUnifiedDiff(['--- a/x', '+++ b/x', '@@ -5 +5 @@', ' old', '+new'].join('\n'))!;

    expect(diff.lines[0]).toMatchObject({ type: 'hunk', oldLineNo: 5, newLineNo: 5 });
    // The leading context row consumed new line 5; the addition lands on 6.
    expect(diff.lines[1]).toMatchObject({ type: 'context', oldLineNo: 5, newLineNo: 5 });
    expect(diff.lines[2]).toMatchObject({ type: 'added', newLineNo: 6 });
  });

  it('handles blank added/removed lines (length-1 rows)', () => {
    const diff = parseUnifiedDiff(['--- a/x', '+++ b/x', '@@ -1,2 +1,3 @@', ' a', '+', '-', '+b'].join('\n'))!;

    expect(diff.lines[1]).toMatchObject({ type: 'context', content: 'a' });
    expect(diff.lines[2]).toMatchObject({ type: 'added', content: '' });
    expect(diff.lines[3]).toMatchObject({ type: 'removed', content: '' });
    expect(diff.addedCount).toBe(2);
    expect(diff.removedCount).toBe(1);
  });

  it('returns null for an empty diff body', () => {
    expect(parseUnifiedDiff('')).toBeNull();
  });
});

describe('parsePatchFileResult / parseWriteFileResult (fail-open)', () => {
  const okResult = JSON.stringify({
    success: true,
    path: 'src/app.py',
    strategy: 'exact',
    matches: 1,
    diff: SAMPLE_DIFF
  });

  it('parses a real patch_file payload', () => {
    const diff = parsePatchFileResult('patch_file', okResult)!;
    expect(diff.addedCount).toBe(2);
  });

  it('fails open on every bad shape', () => {
    expect(parsePatchFileResult('patch_file', 'not json at all')).toBeNull();
    expect(parsePatchFileResult('patch_file', JSON.stringify({ success: true }))).toBeNull();
    expect(parsePatchFileResult('patch_file', JSON.stringify({ error: 'no match' }))).toBeNull();
    expect(parsePatchFileResult('patch_file', JSON.stringify({ diff: '' }))).toBeNull();
    expect(parsePatchFileResult('read_file', okResult)).toBeNull();
    expect(parsePatchFileResult(undefined, okResult)).toBeNull();
    expect(parsePatchFileResult('patch_file', 'null')).toBeNull();
  });

  it('routes write_file through its own gate', () => {
    expect(parseWriteFileResult('write_file', okResult)).not.toBeNull();
    expect(parseWriteFileResult('patch_file', okResult)).toBeNull();
    expect(parseToolDiff('write_file', okResult)).not.toBeNull();
    expect(parseToolDiff('terminal', okResult)).toBeNull();
  });

  it('detects the new-file flag only for write_file', () => {
    expect(writeReportedNewFile('write_file', JSON.stringify({ success: true, isNew: true, diff: null }))).toBe(true);
    expect(writeReportedNewFile('write_file', JSON.stringify({ success: true, isNew: false, diff: null }))).toBe(false);
    expect(writeReportedNewFile('write_file', 'garbage')).toBe(false);
    expect(writeReportedNewFile('patch_file', JSON.stringify({ isNew: true }))).toBe(false);
  });
});

describe('allAddedDiff (new-file preview)', () => {
  it('marks every line added with 1-based new numbers', () => {
    const diff = allAddedDiff('line one\nline two')!;

    expect(diff.addedCount).toBe(2);
    expect(diff.removedCount).toBe(0);
    expect(diff.lines).toEqual([
      { type: 'added', content: 'line one', newLineNo: 1 },
      { type: 'added', content: 'line two', newLineNo: 2 }
    ]);
  });

  it('drops the trailing-newline phantom row', () => {
    const diff = allAddedDiff('one\ntwo\n')!;
    expect(diff.addedCount).toBe(2);
  });

  it('returns null for empty content', () => {
    expect(allAddedDiff('')).toBeNull();
  });
});
