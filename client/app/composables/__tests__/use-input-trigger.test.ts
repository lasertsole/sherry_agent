/** Composer trigger parsing/scoring (pure functions). */

import { describe, expect, it } from 'vitest';

import { detectTrigger, scoreTriggerMatch } from '../use-input-trigger';

describe('detectTrigger', () => {
  it('detects a skill trigger after the start or whitespace', () => {
    expect(detectTrigger('$rev', 4)).toEqual({ kind: 'skill', query: 'rev', tokenLength: 4 });
    expect(detectTrigger('use $review', 11)).toEqual({ kind: 'skill', query: 'review', tokenLength: 7 });
  });

  it('detects a slash command and normalizes the yen aliases', () => {
    expect(detectTrigger('/rev', 4)).toEqual({ kind: 'slash', query: 'rev', tokenLength: 4 });
    expect(detectTrigger('¥rev', '¥rev'.length)).toEqual({
      kind: 'skill',
      query: 'rev',
      tokenLength: 4
    });
  });

  it('does not fire mid-word or after a completed token', () => {
    expect(detectTrigger('foo$bar', 7)).toBeNull();
    expect(detectTrigger('$review ', 8)).toBeNull();
    expect(detectTrigger('plain text', 10)).toBeNull();
  });
});

describe('scoreTriggerMatch', () => {
  it('ranks exact, prefix, substring and description matches', () => {
    expect(scoreTriggerMatch('review', '', 'review')).toBe(1000);
    expect(scoreTriggerMatch('review', '', 'rev')).toBeGreaterThan(300);
    expect(scoreTriggerMatch('code-review', '', 'review')).toBeGreaterThan(100);
    expect(scoreTriggerMatch('review', 'security review', 'security')).toBeGreaterThan(0);
    expect(scoreTriggerMatch('review', '', 'deploy')).toBe(-1);
  });
});
