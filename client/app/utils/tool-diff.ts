/**
 * Inline diff previews for tool results: `patch_file` and `write_file` return
 * (or carry, in write_file's JSON) a Python `difflib.unified_diff` string, and
 * the tool card renders it as a colored diff instead of a raw JSON blob.
 *
 * The parser is pure prefix classification pinned to difflib's EXACT shape:
 * lines all end with `\n` (keepends=True + "".join on the backend), the header
 * pair `--- a/…` / `+++ b/…` precedes `@@ -l,c +l,c @@` hunk headers whose
 * counts are OMITTED when 1, and a blank added/removed line is a length-1 line
 * (`+` / `-` with nothing after it). Anything unrecognized degrades — the
 * caller falls back to the plain `<pre>` rendering on a null result.
 *
 * @module utils/tool-diff
 */

/** One classified diff line (numbers are the gutter columns; a hunk row carries its start). */
export interface DiffLine {
  type: 'added' | 'removed' | 'context' | 'hunk';
  /** The line without its difflib prefix character. */
  content: string;
  oldLineNo?: number;
  newLineNo?: number;
}

/** A parsed diff plus the header counts for the `+N −M` badge. */
export interface ToolDiff {
  lines: DiffLine[];
  addedCount: number;
  removedCount: number;
}

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/**
 * Parse a unified diff body (the backend's `difflib` output) into render rows.
 * @param diff The raw unified diff string.
 * @returns null when the body holds no renderable lines.
 */
export function parseUnifiedDiff(diff: string): ToolDiff | null {
  const rawLines = diff.split('\n');
  if (rawLines.length > 0 && rawLines[rawLines.length - 1] === '') rawLines.pop();

  const lines: DiffLine[] = [];
  let addedCount = 0;
  let removedCount = 0;
  let oldNo: number | null = null;
  let newNo: number | null = null;

  for (const raw of rawLines) {
    if (raw.startsWith('--- ') || raw.startsWith('+++ ')) continue; // file headers
    if (raw.startsWith('@@')) {
      const match = HUNK_HEADER.exec(raw);
      if (match) {
        oldNo = Number(match[1]);
        newNo = Number(match[3]);
      }
      lines.push({ type: 'hunk', content: raw, oldLineNo: oldNo ?? undefined, newLineNo: newNo ?? undefined });
      continue;
    }

    const prefix = raw.charAt(0);
    const content = raw.slice(1);
    if (raw === '') continue; // malformed blank inside a hunk: render nothing
    if (prefix === '+') {
      addedCount++;
      lines.push({ type: 'added', content, newLineNo: newNo ?? undefined });
      if (newNo !== null) newNo++;
    } else if (prefix === '-') {
      removedCount++;
      lines.push({ type: 'removed', content, oldLineNo: oldNo ?? undefined });
      if (oldNo !== null) oldNo++;
    } else if (prefix === ' ') {
      lines.push({ type: 'context', content, oldLineNo: oldNo ?? undefined, newLineNo: newNo ?? undefined });
      if (oldNo !== null) oldNo++;
      if (newNo !== null) newNo++;
    } else {
      // A diff line never reaches here with a real difflib payload; degrade to
      // context rather than dropping content.
      lines.push({ type: 'context', content: raw, oldLineNo: oldNo ?? undefined, newLineNo: newNo ?? undefined });
      if (oldNo !== null) oldNo++;
      if (newNo !== null) newNo++;
    }
  }

  if (lines.length === 0) return null;
  return { lines, addedCount, removedCount };
}

/**
 * The diff payload extractor shared by both tools: the card's result must be
 * JSON carrying a non-empty `diff` string. `{"error": …}` failures, plain
 * text results and empty diffs all fail-open to null.
 * @param toolResult The tool row's result string.
 */
function parseDiffPayload(toolResult: string): ToolDiff | null {
  let payload: unknown;
  try {
    payload = JSON.parse(toolResult);
  } catch {
    return null;
  }
  if (!payload || typeof payload !== 'object') return null;
  const diff = (payload as { diff?: unknown }).diff;
  if (typeof diff !== 'string' || diff.length === 0) return null;
  return parseUnifiedDiff(diff);
}

/**
 * Parse a `patch_file` result into a diff (the backend already computes it).
 * @param toolName The tool row's name (the fast gate against mis-parsing).
 * @param toolResult The result string.
 */
export function parsePatchFileResult(toolName: string | undefined, toolResult: string): ToolDiff | null {
  if (toolName !== 'patch_file') return null;
  return parseDiffPayload(toolResult);
}

/**
 * Parse a `write_file` result into a diff (present when an existing text file
 * was overwritten or appended; null for creations and binary targets).
 * @param toolName The tool row's name.
 * @param toolResult The result string (JSON since the diff return change).
 */
export function parseWriteFileResult(toolName: string | undefined, toolResult: string): ToolDiff | null {
  if (toolName !== 'write_file') return null;
  return parseDiffPayload(toolResult);
}

/**
 * Whether a `write_file` result reports a brand-new file (the backend's
 * `isNew` flag) — the card renders a badge plus an all-added preview then.
 * @param toolName The tool row's name.
 * @param toolResult The result string.
 */
export function writeReportedNewFile(toolName: string | undefined, toolResult: string): boolean {
  if (toolName !== 'write_file') return false;
  try {
    return (JSON.parse(toolResult) as { isNew?: unknown }).isNew === true;
  } catch {
    return false;
  }
}

/**
 * Build an all-added diff for a NEW file's content (every line green, no
 * removed/context rows).
 * @param text The file's full new content (the write's `text` argument).
 */
export function allAddedDiff(text: string): ToolDiff | null {
  const lines = text.split('\n');
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  // '' means NO content at all (a file holding one blank line is ['',''] and
  // legitimately renders as a single added empty row).
  if (lines.length === 0 || (lines.length === 1 && lines[0] === '')) return null;
  return {
    lines: lines.map((content, index) => ({ type: 'added' as const, content, newLineNo: index + 1 })),
    addedCount: lines.length,
    removedCount: 0
  };
}

/**
 * The card's single dispatch entry: pick the right parser for the tool, null
 * for everything else (the plain `<pre>` rendering).
 * @param toolName The tool row's name.
 * @param toolResult The result string.
 */
export function parseToolDiff(toolName: string | undefined, toolResult: string): ToolDiff | null {
  if (toolName === 'patch_file') return parseDiffPayload(toolResult);
  if (toolName === 'write_file') return parseDiffPayload(toolResult);
  return null;
}
