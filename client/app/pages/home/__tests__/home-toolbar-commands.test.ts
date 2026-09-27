import { describe, it, expect, vi } from 'vitest';
import { headerTools } from '../config';
import { buildHomeToolbarCommands, HOME_DIALOG_IDS, HOME_TOOLBAR_EVENTS, type HomeDialogId } from '../dialogs';
import type { RightSidebarPanelKind } from '~/stores/right-sidebar';

/**
 * Every toolbar entry that must open a right-sidebar tab, and the panel kind it
 * must open: the three viewers plus all seven settings-menu editors.
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
  ['extend', 'extend']
];

/** The one entry that still opens a dialog, and the dialog it must open. */
const DIALOG_EVENTS: ReadonlyArray<[string, HomeDialogId]> = [['notification', 'notification']];

function buildCommands() {
  return buildHomeToolbarCommands({
    openDialog: vi.fn(),
    openRightTab: vi.fn()
  });
}

describe('buildHomeToolbarCommands', () => {
  it('routes every viewer and settings entry to its own right-sidebar tab', () => {
    const openDialog = vi.fn();
    const openRightTab = vi.fn();
    const commands = buildHomeToolbarCommands({ openDialog, openRightTab });

    for (const [event, kind] of TAB_EVENTS) {
      openDialog.mockClear();
      openRightTab.mockClear();

      commands[event]?.();

      expect(openRightTab).toHaveBeenCalledTimes(1);
      expect(openRightTab).toHaveBeenCalledWith(kind);
      expect(openDialog).not.toHaveBeenCalled();
    }
  });

  it('routes the notification list to its dialog instead of a tab', () => {
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

  it('registers exactly the toolbar event vocabulary', () => {
    expect(Object.keys(buildCommands()).sort()).toEqual([...HOME_TOOLBAR_EVENTS].sort());
  });

  it('covers every nine-grid header tool plus the top-bar-only bell', () => {
    const commands = buildCommands();

    for (const tool of headerTools) expect(commands).toHaveProperty(tool.event);
    // The log viewer is a nine-grid entry like the other tools …
    expect(headerTools.map(tool => tool.event)).toContain('logs');
    // … while the notification list stays the one top-bar-only command.
    expect(commands).toHaveProperty('notification');
  });

  it('keeps the dialog registry down to the entries that really are dialogs', () => {
    for (const [, dialog] of DIALOG_EVENTS) expect(HOME_DIALOG_IDS).toContain(dialog);
    // Tab entries must NOT be dialogs any more (no dead registry rows), and no
    // tab kind may hide in the dialog registry either.
    for (const [event, kind] of TAB_EVENTS) {
      expect(HOME_DIALOG_IDS).not.toContain(event);
      expect(HOME_DIALOG_IDS).not.toContain(kind);
    }
  });

  it('is a no-op registry entry for an unknown event', () => {
    const commands = buildCommands();

    expect(commands['does-not-exist']).toBeUndefined();

    const openDialog = vi.fn();
    const openRightTab = vi.fn();
    buildHomeToolbarCommands({ openDialog, openRightTab })['does-not-exist']?.();
    expect(openDialog).not.toHaveBeenCalled();
    expect(openRightTab).not.toHaveBeenCalled();
  });
});
