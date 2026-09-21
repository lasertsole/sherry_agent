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
    // subagent-sync.ts keeps the default reserved set: the tasks route has not been handled there.
    expect(RESERVED_SESSION_SEGMENTS).toEqual(['home']);
    expect(sessionIdFromPathname('/home/tasks')).toBe('tasks');
    // stores/todo.ts also reserves the standalone background-tasks route.
    expect(sessionIdFromPathname('/home/tasks', ['home', 'tasks'])).toBeUndefined();
    expect(sessionIdFromPathname('/home/tasks/sess-9', ['home', 'tasks'])).toBe('sess-9');
  });
});
