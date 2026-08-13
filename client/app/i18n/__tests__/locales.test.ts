import { describe, expect, it } from 'vitest';
import zh from '../locales/zh.json';
import en from '../locales/en.json';

type Dict = { [key: string]: unknown };

/** 扁平化嵌套对象为点路径 key 列表，用于对齐校验 */
const flatten = (obj: Dict, prefix = ''): string[] =>
  Object.keys(obj).flatMap((k) => {
    const path = prefix ? `${prefix}.${k}` : k;
    const value = obj[k];
    return value !== null && typeof value === 'object'
      ? flatten(value as Dict, path)
      : [path];
  });

/**
 * 校验插值占位符（{xxx}）对齐。
 * 返回每个叶子 key 的占位符集合。
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
        placeholders.add(match[1]);
      }
      result[path] = placeholders;
    }
  }
  return result;
};

describe('i18n locale parity', () => {
  it('zh.json 与 en.json 拥有完全一致的 key 结构', () => {
    const zhKeys = flatten(zh as Dict).sort();
    const enKeys = flatten(en as Dict).sort();

    expect(zhKeys).toEqual(enKeys);
  });

  it('zh.json 与 en.json 的插值占位符保持一致', () => {
    const zhPlaceholders = placeholdersOf(zh as Dict);
    const enPlaceholders = placeholdersOf(en as Dict);

    for (const key of Object.keys(zhPlaceholders)) {
      expect([...zhPlaceholders[key]].sort()).toEqual([...enPlaceholders[key]].sort());
    }
  });

  it('zh.json 与 en.json 的根命名空间保持一致', () => {
    expect(Object.keys(zh as Dict).sort()).toEqual(
      Object.keys(en as Dict).sort(),
    );
  });
});
