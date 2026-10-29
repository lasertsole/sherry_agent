/**
 * The command palette's static command sources (unit).
 *
 * Contract: the palette's rows are DERIVED from the same grids the menu dialog
 * renders (`headerTools` / `toolboxTools` / the bell), so adding a tile adds a
 * command without touching the palette (the plan's F2). Each handler dispatches
 * the toolbar command registry, not a second copy of the routing.
 */
import { describe, it, expect, vi } from 'vitest';
import { headerTools, toolboxTools } from '../config';
import { buildNotificationCommandEntries, buildSessionCommandEntries, buildToolbarCommandEntries } from '../commands';
import type { RightSidebarPanelKind } from '~/stores/right-sidebar';

/**
 * Identity translator: the label is then the key, which is what we assert.
 * @param key
 */
const t = (key: string): string => key;

describe('buildToolbarCommandEntries', () => {
  it('emits one command per grid and toolbox entry, in grid order', () => {
    const entries = buildToolbarCommandEntries({ openRightTab: vi.fn() }, t);

    expect(entries.map(entry => entry.id)).toEqual(
      [...headerTools, ...toolboxTools].map(tool => `toolbar.${tool.event}`)
    );
    expect(entries.map(entry => entry.label)).toEqual([...headerTools, ...toolboxTools].map(tool => tool.toolName));
  });

  it('files the two toolbox surfaces under navigation and the rest under tools', () => {
    const entries = buildToolbarCommandEntries({ openRightTab: vi.fn() }, t);
    const section = (id: string) => entries.find(entry => entry.id === id)?.section;

    expect(section('toolbar.browser')).toBe('navigate');
    expect(section('toolbar.terminal')).toBe('navigate');
    expect(section('toolbar.systemConfig')).toBe('tools');
    expect(section('toolbar.skills')).toBe('tools');
  });

  it('routes a click through the shared toolbar registry (one openRightTab per kind)', () => {
    const openRightTab = vi.fn();
    const entries = buildToolbarCommandEntries({ openRightTab }, t);
    const byEvent = new Map([...headerTools, ...toolboxTools].map(tool => [tool.event, tool]));

    // Every non-twin entry opens exactly its own panel kind.
    for (const entry of entries) {
      const event = entry.id.replace('toolbar.', '') as string;
      const kind = byEvent.get(event)!.event as RightSidebarPanelKind;
      if (kind === 'browser' || kind === 'terminal') continue;
      openRightTab.mockClear();
      entry.handler();
      expect(openRightTab).toHaveBeenCalledWith(kind);
    }
  });

  it('keeps the event name and title as match keywords', () => {
    const entries = buildToolbarCommandEntries({ openRightTab: vi.fn() }, t);
    const systemConfig = entries.find(entry => entry.id === 'toolbar.systemConfig')!;

    expect(systemConfig.keywords).toContain('systemConfig');
  });
});

describe('buildNotificationCommandEntries', () => {
  it('registers the bell as a navigation command backed by the same registry', () => {
    const openRightTab = vi.fn();
    const [entry] = buildNotificationCommandEntries({ openRightTab }, t);

    expect(entry.id).toBe('toolbar.notification');
    expect(entry.section).toBe('navigate');
    expect(entry.label).toBe('toolbar.notification');

    entry.handler();
    expect(openRightTab).toHaveBeenCalledWith('notification');
  });
});

describe('buildSessionCommandEntries', () => {
  it('runs the new-session store opener and carries a localized label', () => {
    const openDialog = vi.fn();
    const [entry] = buildSessionCommandEntries(openDialog, t);

    expect(entry.id).toBe('session.new');
    expect(entry.section).toBe('session');
    expect(entry.label).toBe('commandPalette.newSession');

    entry.handler();
    expect(openDialog).toHaveBeenCalledTimes(1);
  });
});
