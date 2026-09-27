import { describe, it, expect, vi } from 'vitest';
import { headerTools } from '../config';
import { buildHomeToolbarCommands, HOME_DIALOG_IDS, HOME_TOOLBAR_EVENTS, type HomeDialogId } from '../dialogs';

/** Every toolbar event that must open a dialog, and the dialog it must open. */
const DIALOG_EVENTS: ReadonlyArray<[string, HomeDialogId]> = [
  ['skills', 'skills'],
  ['systemConfig', 'systemConfig'],
  ['persona', 'persona'],
  ['memory', 'memory'],
  ['heartbeat', 'heartbeat'],
  ['cron', 'cron'],
  ['notification', 'notification'],
  ['extend', 'extend']
];

/** Events that open a right-sidebar tab instead of a dialog or a page. */
const RIGHT_TAB_EVENTS: ReadonlyArray<[string, 'logs' | 'stats' | 'knowledgeGraph']> = [
  ['logs', 'logs'],
  ['stats', 'stats'],
  ['knowledgeGraph', 'knowledgeGraph']
];

function buildCommands() {
  return buildHomeToolbarCommands({
    openDialog: vi.fn(),
    openRightTab: vi.fn()
  });
}

describe('buildHomeToolbarCommands', () => {
  it('routes every dialog event to its own dialog', () => {
    const openDialog = vi.fn();
    const openRightTab = vi.fn();
    const commands = buildHomeToolbarCommands({ openDialog, openRightTab });

    for (const [event, dialog] of DIALOG_EVENTS) {
      openDialog.mockClear();
      openRightTab.mockClear();

      commands[event]?.();

      expect(openDialog).toHaveBeenCalledTimes(1);
      expect(openDialog).toHaveBeenCalledWith(dialog);
      expect(openRightTab).not.toHaveBeenCalled();
    }
  });

  it('routes logs, stats and knowledgeGraph to right-sidebar tabs instead of dialogs', () => {
    const openDialog = vi.fn();
    const openRightTab = vi.fn();
    const commands = buildHomeToolbarCommands({ openDialog, openRightTab });

    for (const [event, kind] of RIGHT_TAB_EVENTS) {
      openDialog.mockClear();
      openRightTab.mockClear();

      commands[event]?.();

      expect(openRightTab).toHaveBeenCalledTimes(1);
      expect(openRightTab).toHaveBeenCalledWith(kind);
      expect(openDialog).not.toHaveBeenCalled();
    }
  });

  it('registers exactly the toolbar event vocabulary', () => {
    expect(Object.keys(buildCommands()).sort()).toEqual([...HOME_TOOLBAR_EVENTS].sort());
  });

  it('covers every nine-grid header tool plus the two top-bar-only buttons', () => {
    const commands = buildCommands();

    for (const tool of headerTools) expect(commands).toHaveProperty(tool.event);
    expect(commands).toHaveProperty('logs');
    expect(commands).toHaveProperty('notification');
  });

  it('maps every dialog event to a registered dialog id', () => {
    for (const [, dialog] of DIALOG_EVENTS) expect(HOME_DIALOG_IDS).toContain(dialog);
    // The tab events must NOT be dialogs any more (no dead registry rows).
    for (const [, kind] of RIGHT_TAB_EVENTS) expect(HOME_DIALOG_IDS).not.toContain(kind);
    // The knowledge graph is not a route any more either: nothing to navigate to.
  });

  it('is a no-op registry entry for an unknown event', () => {
    const commands = buildCommands();

    expect(commands['does-not-exist']).toBeUndefined();

    const openDialog = vi.fn();
    const navigateToKnowledgeGraph = vi.fn();
    buildHomeToolbarCommands({ openDialog, navigateToKnowledgeGraph })['does-not-exist']?.();
    expect(openDialog).not.toHaveBeenCalled();
    expect(navigateToKnowledgeGraph).not.toHaveBeenCalled();
  });
});
