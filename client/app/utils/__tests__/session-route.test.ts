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
    expect(RESERVED_SESSION_SEGMENTS).toEqual(['home']);
    // The reserved set is a caller decision: the shell route is the only one
    // every consumer agrees on, and others may add their own.
    expect(sessionIdFromPathname('/home/other', ['home', 'other'])).toBeUndefined();
    expect(sessionIdFromPathname('/home/other/sess-9', ['home', 'other'])).toBe('sess-9');
  });
});
