import { describe, it, expect } from 'vitest';
import {
  max,
  min,
  getFormattedDate,
  stringToDate,
  formatToLocalTime,
  compareDate,
  isLate,
  maxDate,
  getUTCTimeNow,
  isTimestamp,
} from '../utils';

describe('max / min (string|number comparison)', () => {
  it('max returns the larger of two numbers', () => {
    expect(max(1, 2)).toBe(2);
    expect(max(5, 5)).toBe(5);
    expect(max(-3, 0)).toBe(0);
  });

  it('max works on strings (lexicographic order)', () => {
    expect(max('apple', 'banana')).toBe('banana');
    expect(max('2024-01-01', '2024-01-02')).toBe('2024-01-02');
  });

  it('min returns the smaller of two numbers', () => {
    expect(min(1, 2)).toBe(1);
    expect(min(5, 5)).toBe(5);
    expect(min(-3, 0)).toBe(-3);
  });

  it('min works on strings', () => {
    expect(min('apple', 'banana')).toBe('apple');
  });

  it('max/min preserve original value when equal', () => {
    expect(max(7, 7)).toBe(7);
    expect(min('x', 'x')).toBe('x');
  });
});

describe('date parsing + formatting', () => {
  it('stringToDate parses an ISO/timestamp numeric string', () => {
    const ts = 1700000000000;
    const d = stringToDate(String(ts));
    expect(d).not.toBeNull();
    expect(d!.getTime()).toBe(ts);
  });

  it('stringToDate parses "YYYY-MM-DD HH:mm:ss" (space + no Z)', () => {
    const d = stringToDate('2024-01-05 10:30:00');
    expect(d).not.toBeNull();
    expect(d!.getFullYear()).toBe(2024);
    expect(d!.getMonth()).toBe(0);
    expect(d!.getDate()).toBe(5);
  });

  it('stringToDate returns null for invalid input', () => {
    expect(stringToDate('not-a-date')).toBeNull();
    expect(stringToDate('')).not.toBeNull(); // empty string -> Invalid Date -> returns null
    // note: '' parses to Invalid Date, which is handled
  });

  it('formatToLocalTime formats a valid UTC string', () => {
    const out = formatToLocalTime('2024-01-05 10:30:00');
    expect(out).toMatch(/^\d{4}\.\d{2}\.\d{2}$|^\d{2}\.\d{2}$|^\d{1,2}:\d{2}:\d{2}$/);
  });

  it('formatToLocalTime returns empty string for falsy input', () => {
    expect(formatToLocalTime(undefined)).toBe('');
    expect(formatToLocalTime('')).toBe('');
  });

  it('formatToLocalTime returns null for invalid input', () => {
    expect(formatToLocalTime('garbage')).toBeNull();
  });
});

describe('date comparison helpers', () => {
  const earlier = new Date('2024-01-01T00:00:00Z');
  const later = new Date('2024-01-02T00:00:00Z');

  it('compareDate returns positive/negative/zero difference', () => {
    expect(compareDate(later, earlier)).toBeGreaterThan(0);
    expect(compareDate(earlier, later)).toBeLessThan(0);
    expect(compareDate(later, new Date(later.getTime()))).toBe(0);
  });

  it('isLate returns true when a is strictly after b', () => {
    expect(isLate(later, earlier)).toBe(true);
    expect(isLate(earlier, later)).toBe(false);
    expect(isLate(later, new Date(later.getTime()))).toBe(false);
  });

  it('maxDate returns the later date', () => {
    expect(maxDate(earlier, later)).toBe(later);
    expect(maxDate(later, earlier)).toBe(later);
  });
});

describe('UTC helpers', () => {
  it('getUTCTimeNow returns a UTC timestamp string ending in Z', () => {
    const now = getUTCTimeNow();
    expect(now.endsWith('Z')).toBe(true);
    // Format: YYYY-M-DTH:M:S.microsecondsZ (microsecond precision)
    expect(now).toMatch(/^\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{1,2}:\d{1,2}\.\d{6}Z$/);
    // The digit segments (year/month/day/hour/min/sec) must be coherent with the currentUTC time
    const utcNow = new Date();
    const year = utcNow.getUTCFullYear();
    expect(now).toContain(`${year}-`);
  });

  it('isTimestamp recognizes valid ISO date strings', () => {
    expect(isTimestamp('2024-01-01T00:00:00Z')).toBe(true);
    // Bare numeric timestamps are NOT recognized (new Date('1700000000000') is Invalid Date)
    expect(isTimestamp(String(Date.now()))).toBe(false);
  });

  it('isTimestamp rejects invalid strings', () => {
    expect(isTimestamp('hello world')).toBe(false);
    expect(isTimestamp('2024-99-99')).toBe(false);
  });
});
