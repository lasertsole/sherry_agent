import { describe, it, expect } from 'vitest';
import { estimateTextTokens } from '../message-items';

describe('estimateTextTokens', () => {
  it('counts pure ASCII at one token per 4 chars', () => {
    expect(estimateTextTokens('abcdefgh')).toBe(2); // 8 / 4
    expect(estimateTextTokens('ab')).toBe(0); // floor of 0.5
  });

  it('counts CJK at one token per 2 chars', () => {
    expect(estimateTextTokens('你好')).toBe(1); // 2 / 2
    expect(estimateTextTokens('你好世界早上好')).toBe(3); // 7 CJK chars → 3
  });

  it('mixes CJK and ASCII with separate tiers', () => {
    // 4 CJK → 2 tokens; 8 ASCII → 2 tokens
    expect(estimateTextTokens('你好世界abcdefgh')).toBe(4);
  });

  it('covers kana and hangul as CJK', () => {
    expect(estimateTextTokens('カナ')).toBe(1);
    expect(estimateTextTokens('한글')).toBe(1);
  });

  it('returns 0 for empty content', () => {
    expect(estimateTextTokens('')).toBe(0);
  });

  it('is monotonic in content length', () => {
    const short = estimateTextTokens('hello world');
    const long = estimateTextTokens('hello world '.repeat(20));
    expect(long).toBeGreaterThan(short);
  });
});
