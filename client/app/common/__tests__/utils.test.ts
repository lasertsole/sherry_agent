import { describe, it, expect } from 'vitest';
import { formatCompactTimeString } from '../utils';

describe('formatCompactTimeString', () => {
  it('renders the default YYYY-MM-DD HH:mm from a 14-digit compact string (minutes, not seconds)', () => {
    // Regression guard: the default format used to be 'YYYY-MM-DD HH:ss',
    // which rendered the SECONDS in the minutes' position ("20:04" for
    // 20:14:04) on every ChatBox message label.
    expect(formatCompactTimeString('20260901201404')).toBe('2026-09-01 20:14');
  });

  it('formats midnight-crossing timestamps correctly', () => {
    expect(formatCompactTimeString('20260907000650')).toBe('2026-09-07 00:06');
  });

  it('honors a custom format', () => {
    expect(formatCompactTimeString('20260901201404', 'YYYY/MM/DD HH:mm:ss')).toBe('2026/09/01 20:14:04');
  });

  it('returns an empty string for malformed input (empty / wrong length / non-numeric)', () => {
    expect(formatCompactTimeString('')).toBe('');
    expect(formatCompactTimeString('2026090120140')).toBe('');
    expect(formatCompactTimeString('2026090120140a')).toBe('');
    // NOTE: out-of-range fields are NOT rejected — dayjs rolls them over
    // (month 13 → next January), so '20261301201404' → '2027-01-01 20:14'.
  });
});
