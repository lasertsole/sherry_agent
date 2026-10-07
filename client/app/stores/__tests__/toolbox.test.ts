/**
 * The toolbox: the browser panel's URL history and the user terminal's
 * scrollback, both PER SESSION.
 *
 * The state lives in a store because the right sidebar unmounts an inactive
 * panel (no KeepAlive) — a terminal whose log vanished on every tab switch, or a
 * browser that forgot the page, would be unusable.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { normalizeUrl, useToolboxStore } from '../toolbox';

describe('toolbox store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('normalizes an address the way an address bar does', () => {
    expect(normalizeUrl('example.com')).toBe('https://example.com');
    expect(normalizeUrl('  http://localhost:8080/x  ')).toBe('http://localhost:8080/x');
    expect(normalizeUrl('https://a.b/c')).toBe('https://a.b/c');
    expect(normalizeUrl('/relative/path')).toBe('/relative/path');
    expect(normalizeUrl('   ')).toBe('');
  });

  it('keeps a navigation history and walks it with back / forward', () => {
    const store = useToolboxStore();

    store.navigate('s1', 'a.com');
    store.navigate('s1', 'b.com');
    store.navigate('s1', 'c.com');
    expect(store.browserFor('s1').url).toBe('https://c.com');
    expect(store.canGoBack('s1')).toBe(true);
    expect(store.canGoForward('s1')).toBe(false);

    store.step('s1', -1);
    expect(store.browserFor('s1').url).toBe('https://b.com');
    expect(store.canGoForward('s1')).toBe(true);

    store.step('s1', 1);
    expect(store.browserFor('s1').url).toBe('https://c.com');

    // A step past either end is a no-op.
    store.step('s1', 1);
    expect(store.browserFor('s1').url).toBe('https://c.com');
    store.step('s1', -1);
    store.step('s1', -1);
    store.step('s1', -1);
    expect(store.browserFor('s1').url).toBe('https://a.com');
    expect(store.canGoBack('s1')).toBe(false);
  });

  it('drops the forward branch when a new address is typed', () => {
    const store = useToolboxStore();

    store.navigate('s1', 'a.com');
    store.navigate('s1', 'b.com');
    store.step('s1', -1);
    store.navigate('s1', 'c.com');

    expect(store.browserFor('s1').history).toEqual(['https://a.com', 'https://c.com']);
    expect(store.canGoForward('s1')).toBe(false);
  });

  it('keeps each session’s browser state separate', () => {
    const store = useToolboxStore();

    store.navigate('s1', 'a.com');
    store.navigate('s2', 'b.com');

    expect(store.browserFor('s1').url).toBe('https://a.com');
    expect(store.browserFor('s2').url).toBe('https://b.com');
    // An untouched session has no page (the empty state).
    expect(store.browserFor('s3').url).toBe('');
  });

  it('accumulates the terminal log per session and clears it on request', () => {
    const store = useToolboxStore();

    store.recordRun('s1', {
      command: 'ls',
      output: 'a.txt',
      exitCode: 0,
      durationMs: 5,
      truncated: false,
      cwd: '/tmp/p'
    });
    store.recordRun('s1', {
      command: 'exit 3',
      output: 'boom',
      exitCode: 3,
      durationMs: 7,
      truncated: false,
      cwd: '/tmp/p'
    });
    store.recordRun('s2', {
      command: 'pwd',
      output: '/other',
      exitCode: 0,
      durationMs: 1,
      truncated: false,
      cwd: '/other'
    });

    expect(store.terminalFor('s1').map(entry => entry.command)).toEqual(['ls', 'exit 3']);
    expect(store.terminalFor('s2').map(entry => entry.command)).toEqual(['pwd']);
    // The prompt directory follows the last run of that session.
    expect(store.terminalCwd['s1']).toBe('/tmp/p');

    store.clearTerminal('s1');
    expect(store.terminalFor('s1')).toEqual([]);
    expect(store.terminalFor('s2')).toHaveLength(1);
  });
});
