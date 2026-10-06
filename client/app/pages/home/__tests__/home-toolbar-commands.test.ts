import { describe, it, expect, vi } from 'vitest';
import { headerTools } from '../config';
import { buildHomeToolbarCommands, HOME_TOOLBAR_EVENTS } from '../dialogs';
import type { RightSidebarPanelKind } from '~/stores/right-sidebar';

/**
 * Every toolbar entry and the panel kind it opens: the three viewers, the seven
 * settings-menu editors and the notification list (a top-bar-only button).
 */
const TAB_EVENTS: ReadonlyArray<[string, RightSidebarPanelKind]> = [
  ['logs', 'logs'],
  ['stats', 'stats'],
  ['knowledgeGraph', 'knowledgeGraph'],
  ['skills', 'skills'],
  ['systemConfig', 'systemConfig'],
  ['persona', 'persona'],
  ['memory', 'memory'],
  ['heartbeat', 'heartbeat'],
  ['cron', 'cron'],
  ['extend', 'extend'],
  ['notification', 'notification']
];

function buildCommands() {
  return buildHomeToolbarCommands({ openRightTab: vi.fn() });
}

describe('buildHomeToolbarCommands', () => {
  it('routes every toolbar entry — notification included — to its own right-sidebar tab', () => {
    const openRightTab = vi.fn();
    const commands = buildHomeToolbarCommands({ openRightTab });

    for (const [event, kind] of TAB_EVENTS) {
      openRightTab.mockClear();

      commands[event]?.();

      expect(openRightTab).toHaveBeenCalledTimes(1);
      expect(openRightTab).toHaveBeenCalledWith(kind);
    }
  });

  it('registers exactly the toolbar event vocabulary', () => {
    expect(Object.keys(buildCommands()).sort()).toEqual([...HOME_TOOLBAR_EVENTS].sort());
  });

  it('covers every nine-grid header tool plus the top-bar-only bell', () => {
    const commands = buildCommands();

    for (const tool of headerTools) expect(commands).toHaveProperty(tool.event);
    // The log viewer is a nine-grid entry like the other tools …
    expect(headerTools.map(tool => tool.event)).toContain('logs');
    // 预设角色 leads the nine-grid (its panel is where a session's identity is
    // set up), and every tile keeps a distinct event.
    expect(headerTools[0]!.event).toBe('persona');
    expect(new Set(headerTools.map(tool => tool.event)).size).toBe(headerTools.length);
    // … while the notification list stays the one top-bar-only command.
    expect(commands).toHaveProperty('notification');
  });

  it('is a no-op registry entry for an unknown event', () => {
    const commands = buildCommands();

    expect(commands['does-not-exist']).toBeUndefined();

    const openRightTab = vi.fn();
    buildHomeToolbarCommands({ openRightTab })['does-not-exist']?.();
    expect(openRightTab).not.toHaveBeenCalled();
  });
});
