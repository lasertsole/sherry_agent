import { describe, it, expect } from 'vitest';
import { RESERVED_SESSION_SEGMENTS, sessionIdFromPathname } from '../session-route';

describe('sessionIdFromPathname', () => {
  it('returns the trailing path segment as the session id', () => {
    expect(sessionIdFromPathname('/home/sess-1')).toBe('sess-1');
    expect(sessionIdFromPathname('/home/sess-1/')).toBe('sess-1');
    expect(sessionIdFromPathname('//home//sess-1//')).toBe('sess-1');
  });

  it('returns undefined when the pathname carries no session id', () => {
    expect(sessionIdFromPathname('/home')).toBeUndefined();
    expect(sessionIdFromPathname('/home/')).toBeUndefined();
    expect(sessionIdFromPathname('/')).toBeUndefined();
    expect(sessionIdFromPathname('')).toBeUndefined();
    expect(sessionIdFromPathname(undefined)).toBeUndefined();
  });

  it('honours caller-specific reserved segments', () => {
    expect(RESERVED_SESSION_SEGMENTS).toEqual(['home', 'meeting']);
    // The reserved set is a caller decision: the shell + conference routes are
    // the shared baseline, and others may add their own.
    expect(sessionIdFromPathname('/home/other', ['home', 'other'])).toBeUndefined();
    expect(sessionIdFromPathname('/home/other/sess-9', ['home', 'other'])).toBe('sess-9');
  });

  it('never treats the meeting route segment as a session id (S4)', () => {
    // `/meeting` is a route, not a session; the trailing meeting id in
    // `/meeting/:id` is still the last segment, so those pages must not feed
    // session-scoped state. Consumers that mount only under /home are safe.
    expect(sessionIdFromPathname('/meeting')).toBeUndefined();
  });
});
