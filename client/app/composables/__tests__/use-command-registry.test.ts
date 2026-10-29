/**
 * The command palette's registry (unit).
 *
 * Contract: registration is additive and idempotent (a repeated id replaces the
 * earlier row), ranking is tiered (exact > prefix > substring > keyword) and
 * `highlightLabel` escapes BEFORE it wraps the match — the palette renders the
 * result with `v-html`, so an unescaped session title or skill name would be an
 * injection point.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import {
  allCommands,
  highlightLabel,
  registerStatic,
  registerStaticBatch,
  resetCommandRegistry,
  scoreCommand,
  searchCommands,
  setDynamicCommands,
  type CommandEntry
} from '../use-command-registry';

/**
 * A command with an inert handler.
 * @param overrides Fields to override.
 */
function command(overrides: Partial<CommandEntry> = {}): CommandEntry {
  return {
    id: 'cmd',
    label: 'Command',
    section: 'tools',
    handler: () => undefined,
    ...overrides
  };
}

describe('use-command-registry', () => {
  beforeEach(() => {
    resetCommandRegistry();
  });

  it('replaces a repeated id instead of appending a duplicate', () => {
    registerStatic(command({ id: 'toolbar.skills', label: '技能' }));
    registerStatic(command({ id: 'toolbar.skills', label: '技能面板' }));

    expect(allCommands()).toHaveLength(1);
    expect(allCommands()[0]!.label).toBe('技能面板');
  });

  it('registers a batch and keeps static commands before dynamic ones', () => {
    registerStaticBatch([command({ id: 'a' }), command({ id: 'b' })]);
    setDynamicCommands([command({ id: 'skill.x', section: 'skills' })]);

    expect(allCommands().map(entry => entry.id)).toEqual(['a', 'b', 'skill.x']);
  });

  it('a re-registered batch replaces its own ids and keeps other commands', () => {
    registerStaticBatch([command({ id: 'a' }), command({ id: 'b' })]);
    registerStaticBatch([command({ id: 'a', label: 'A2' }), command({ id: 'c' })]);

    expect(allCommands().map(entry => `${entry.id}:${entry.label}`)).toEqual(['b:Command', 'a:A2', 'c:Command']);
  });

  it('scores exact over prefix over substring over keyword', () => {
    const entry = command({ label: 'Browser', keywords: ['web'] });

    expect(scoreCommand(entry, 'browser')).toBe(1000);
    expect(scoreCommand(entry, 'bro')).toBeGreaterThan(scoreCommand(entry, 'owser'));
    expect(scoreCommand(entry, 'owser')).toBeGreaterThan(scoreCommand(entry, 'web'));
    expect(scoreCommand(entry, 'nothing')).toBe(-1);
  });

  it('an empty query matches everything with the same score', () => {
    expect(scoreCommand(command({ label: 'anything' }), '')).toBe(1);
  });

  it('ranks matches best-first and drops the misses', () => {
    const entries = [
      command({ id: 'a', label: 'Terminal' }),
      command({ id: 'b', label: 'Terminal settings' }),
      command({ id: 'c', label: 'Memory', keywords: ['terminal'] }),
      command({ id: 'd', label: 'Browser' })
    ];

    expect(searchCommands('terminal', entries).map(scored => scored.entry.id)).toEqual(['a', 'b', 'c']);
  });

  it('trims and lowercases the query before matching', () => {
    const entries = [command({ id: 'a', label: 'New Session' })];

    expect(searchCommands('  NEW  ', entries).map(scored => scored.entry.id)).toEqual(['a']);
  });

  it('returns an empty list (never throws) when nothing matches', () => {
    expect(searchCommands('zzz', [command()])).toEqual([]);
  });
});

describe('highlightLabel', () => {
  it('wraps the matched substring in <mark> and keeps the rest', () => {
    expect(highlightLabel('System Settings', 'set')).toBe('System <mark>Set</mark>tings');
  });

  it('escapes markup in the label so v-html cannot inject', () => {
    const label = '<img src=x onerror=alert(1)>';
    const html = highlightLabel(label, 'img');

    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;');
    expect(html).toContain('<mark>img</mark>');
  });

  it('escapes the MATCHED text too, not only the untouched parts', () => {
    expect(highlightLabel('a<b', '<')).toBe('a<mark>&lt;</mark>b');
  });

  it('escapes an empty query unchanged and never marks', () => {
    expect(highlightLabel('<b>', '')).toBe('&lt;b&gt;');
  });

  it('escapes a non-matching label without a mark', () => {
    expect(highlightLabel('Plain', 'zzz')).toBe('Plain');
  });
});
