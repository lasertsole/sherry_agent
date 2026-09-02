import { describe, expect, it } from 'vitest';
import zh from '../../i18n/locales/zh.json';
import en from '../../i18n/locales/en.json';
import ja from '../../i18n/locales/ja.json';
import ko from '../../i18n/locales/ko.json';

type Dict = { [key: string]: unknown };

/** Flattens a nested object into a list of dot-path keys, used for parity checks */
const flatten = (obj: Dict, prefix = ''): string[] =>
  Object.keys(obj).flatMap(k => {
    const path = prefix ? `${prefix}.${k}` : k;
    const value = obj[k];
    return value !== null && typeof value === 'object' ? flatten(value as Dict, path) : [path];
  });

/**
 * Validates that interpolation placeholders ({xxx}) line up.
 * Returns the placeholder set for every leaf key.
 */
const placeholdersOf = (obj: Dict, prefix = ''): Record<string, Set<string>> => {
  const result: Record<string, Set<string>> = {};
  for (const k of Object.keys(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    const value = obj[k];
    if (value !== null && typeof value === 'object') {
      Object.assign(result, placeholdersOf(value as Dict, path));
    } else {
      const placeholders = new Set<string>();
      for (const match of String(value).matchAll(/\{(\w+)\}/g)) {
        placeholders.add(match[1]!);
      }
      result[path] = placeholders;
    }
  }
  return result;
};

describe('i18n locale parity', () => {
  const others: Dict[] = [zh, en, ja, ko];

  it('所有语言与 zh.json 拥有完全一致的 key 结构', () => {
    const zhKeys = flatten(zh as Dict).sort();
    for (const other of others) {
      expect(flatten(other).sort()).toEqual(zhKeys);
    }
  });

  it('所有语言与 zh.json 的插值占位符保持一致', () => {
    for (const other of others) {
      const otherPlaceholders = placeholdersOf(other as Dict);
      for (const key of Object.keys(otherPlaceholders)) {
        expect([...otherPlaceholders[key]!].sort()).toEqual([...placeholdersOf(zh as Dict)[key]!].sort());
      }
    }
  });

  it('所有语言与 zh.json 的根命名空间保持一致', () => {
    const zhNamespaces = Object.keys(zh as Dict).sort();
    for (const other of others) {
      expect(Object.keys(other).sort()).toEqual(zhNamespaces);
    }
  });
});

describe('i18n chat.backgroundMessage (subagent-origin-tagging Task 5)', () => {
  /** Resolves a dot-path against a locale dict (index-signature safe, no literal-type pitfalls) */
  const keyOf = (dict: Dict, path: string): unknown =>
    path.split('.').reduce<unknown>((node, part) => (node && typeof node === 'object' ? (node as Dict)[part] : undefined), dict);

  // Fixed copy from the plan: the muted label shown above background-task carrier cards
  it('all four locales define chat.backgroundMessage with the planned copy', () => {
    expect(keyOf(en as Dict, 'chat.backgroundMessage')).toBe('Background task');
    expect(keyOf(zh as Dict, 'chat.backgroundMessage')).toBe('后台任务');
    expect(keyOf(ja as Dict, 'chat.backgroundMessage')).toBe('バックグラウンドタスク');
    expect(keyOf(ko as Dict, 'chat.backgroundMessage')).toBe('백그라운드 작업');
  });
});
