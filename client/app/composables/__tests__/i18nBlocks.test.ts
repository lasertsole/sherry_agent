import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en.json';
import ja from '../../i18n/locales/ja.json';
import ko from '../../i18n/locales/ko.json';
import zh from '../../i18n/locales/zh.json';

type Dict = { [key: string]: unknown };
type Locale = 'en' | 'zh' | 'ja' | 'ko';

const LOCALES: Locale[] = ['en', 'zh', 'ja', 'ko'];

/** Central locale dictionaries, keyed by locale code. */
const CENTRAL: Record<Locale, Dict> = { en, zh, ja, ko };
const CENTRAL_KEYS = LOCALES.map(locale => new Set(flatten(CENTRAL[locale]!)));

// Flattens a nested object into a list of dot-path keys, used for parity checks.
function flatten(obj: Dict, prefix = ''): string[] {
  return Object.keys(obj).flatMap(k => {
    const keyPath = prefix ? `${prefix}.${k}` : k;
    const value = obj[k];
    return value !== null && typeof value === 'object' ? flatten(value as Dict, keyPath) : [keyPath];
  });
}

const APP_DIR = path.join(process.cwd(), 'app');

// Recursively collects every `.vue` file under a directory.
function collectVueFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) return collectVueFiles(fullPath);
    return entry.isFile() && entry.name.endsWith('.vue') ? [fullPath] : [];
  });
}

/** `<i18n lang="json"> … </i18n>` custom blocks, parsed into per-locale dicts. */
const I18N_BLOCK_RE = /<i18n[^>]*lang="json"[^>]*>\s*([\s\S]*?)\s*<\/i18n>/g;

function parseInlineBlocks(source: string): Record<string, Dict>[] {
  return [...source.matchAll(I18N_BLOCK_RE)].map(block => JSON.parse(block[1]!) as Record<string, Dict>);
}

// Static `t('key')` / `tm('key')` references. The lookbehind rejects method-name
// false positives (`emit('send')`, `split('-')`, `getContext('2d')`, …) and the
// quoted key charset skips dynamic keys (template literals, variables).
const STATIC_T_RE = /(?<![A-Za-z0-9_])t\(\s*['"]([A-Za-z0-9_.-]+)['"]/g;
const STATIC_TM_RE = /(?<![A-Za-z0-9_])tm\(\s*['"]([A-Za-z0-9_.-]+)['"]/g;

function staticKeysOf(source: string): string[] {
  return [
    ...[...source.matchAll(STATIC_T_RE)].map(match => match[1]!),
    ...[...source.matchAll(STATIC_TM_RE)].map(match => match[1]!)
  ];
}

const rel = (file: string): string => path.relative(process.cwd(), file);

const vueFiles = collectVueFiles(APP_DIR).map(file => {
  const source = readFileSync(file, 'utf8');
  return { file, source, blocks: parseInlineBlocks(source) };
});

describe('i18n inline block parity (SFC <i18n lang="json">)', () => {
  const filesWithBlocks = vueFiles.filter(entry => entry.blocks.length > 0);

  it('每个内联块都必须声明 en/zh/ja/ko 四语', () => {
    for (const { file, blocks } of filesWithBlocks) {
      blocks.forEach((block, index) => {
        expect(Object.keys(block).sort(), `${rel(file)} block #${index + 1}`).toEqual([...LOCALES].sort());
      });
    }
  });

  it('同一块内四语的扁平化键结构完全一致', () => {
    for (const { file, blocks } of filesWithBlocks) {
      blocks.forEach((block, index) => {
        const reference = flatten(block.en ?? {}).sort();
        for (const locale of LOCALES) {
          expect(flatten(block[locale] ?? {}).sort(), `${rel(file)} block #${index + 1} ${locale}`).toEqual(reference);
        }
      });
    }
  });
});

describe('i18n static key resolution', () => {
  it('组件内静态 t()/tm() 键必须在内联块或中央四语 JSON 中可解析', () => {
    const violations: string[] = [];
    for (const { file, source, blocks } of vueFiles) {
      const inlineKeys = new Set(blocks.flatMap(block => LOCALES.flatMap(locale => flatten(block[locale] ?? {}))));
      for (const key of staticKeysOf(source)) {
        const inAllCentral = CENTRAL_KEYS.every(keys => keys.has(key));
        if (!inlineKeys.has(key) && !inAllCentral) violations.push(`${rel(file)}: ${key}`);
      }
    }
    expect(violations).toEqual([]);
  });
});
