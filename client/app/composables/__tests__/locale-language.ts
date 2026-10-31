/**
 * Locale-value language gate (shared by the central-locale and SFC-block tests).
 *
 * The four locale dictionaries must carry the RIGHT language in every value, not
 * just the same keys: a Chinese label in `en.json` (live-verified once:
 * `config.agent.readonlyHint`) or Simplified Chinese inside a ja/ko value is the
 * same leak the docs gate catches in the READMEs, one layer down in the product.
 *
 * The Simplified-only character list lives in `scripts/data/simplified_chinese_only.txt`
 * — the SAME file `scripts/check_doc_language.py` reads, so the two gates cannot
 * drift apart.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';

/** CJK ideographs (the English rule bans all of them). */
export const CJK_RUN = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+/g;

/** Language names a value may spell in its own script (the switcher labels). */
export const ALLOWED_TOKENS: ReadonlyArray<string> = ['简体中文', '中文', '日本語', '한국어'];

/** The shared list: Simplified-only characters (never used in Japanese/Korean). */
export const SIMPLIFIED_ONLY: ReadonlySet<string> = (() => {
  // A relative path off `process.cwd()` (the client package root, like
  // `i18nBlocks.test.ts`): the test environment's `import.meta.url` is not a
  // file URL, so it cannot resolve the path.
  const file = path.resolve(process.cwd(), '..', 'scripts', 'data', 'simplified_chinese_only.txt');
  const raw = readFileSync(file, 'utf8');
  const chars = raw
    .split('\n')
    .filter(line => !line.trim().startsWith('#'))
    .join('');
  const set = new Set(chars.split('').filter(char => /\S/.test(char)));
  if (set.size === 0) throw new Error(`no Simplified-only characters found in ${file}`);
  return set;
})();

/**
 * Strip the allow-listed language names (they legitimately appear in any value).
 * @param value Locale value.
 */
function scrubbed(value: string): string {
  let text = value;
  for (const token of ALLOWED_TOKENS) text = text.split(token).join(' ');
  return text;
}

/**
 * CJK runs that must not appear in an English value.
 * @param value Locale value.
 * @returns The offending runs (empty = clean).
 */
export function cjkRunsIn(value: string): string[] {
  return scrubbed(value).match(CJK_RUN) ?? [];
}

/**
 * Simplified-only characters that must not appear in a Japanese/Korean value.
 * @param value Locale value.
 * @returns The offending characters, sorted and deduplicated (empty = clean).
 */
export function simplifiedOnlyIn(value: string): string[] {
  const text = scrubbed(value);
  return [...new Set([...text].filter(char => SIMPLIFIED_ONLY.has(char)))].sort();
}

/** One `[dot.path, value]` pair of a locale dictionary. */
export interface LocaleLeaf {
  path: string;
  value: string;
}

/**
 * Flatten a locale dictionary into string leaves (dot paths).
 * @param node Sub-tree to walk.
 * @param prefix Path prefix.
 */
export function localeLeaves(node: unknown, prefix = ''): LocaleLeaf[] {
  if (typeof node === 'string') return [{ path: prefix, value: node }];
  if (node === null || typeof node !== 'object') return [];
  return Object.entries(node as Record<string, unknown>).flatMap(([key, child]) =>
    localeLeaves(child, prefix ? `${prefix}.${key}` : key)
  );
}
