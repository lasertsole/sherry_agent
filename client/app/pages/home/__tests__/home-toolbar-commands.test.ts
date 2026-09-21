import { describe, it, expect, vi } from 'vitest';
import { headerTools } from '../config';
import { buildHomeToolbarCommands, HOME_DIALOG_IDS, HOME_TOOLBAR_EVENTS, type HomeDialogId } from '../dialogs';

/** Every toolbar event that must open a dialog, and the dialog it must open. */
const DIALOG_EVENTS: ReadonlyArray<[string, HomeDialogId]> = [
  ['skills', 'skills'],
  ['stats', 'stats'],
  ['systemConfig', 'systemConfig'],
  ['persona', 'persona'],
  ['memory', 'memory'],
  ['heartbeat', 'heartbeat'],
  ['cron', 'cron'],
  ['logs', 'logs'],
  ['notification', 'notification'],
  ['extend', 'extend']
];

function buildCommands() {
  return buildHomeToolbarCommands({ openDialog: vi.fn(), navigateToKnowledgeGraph: vi.fn() });
}

describe('buildHomeToolbarCommands', () => {
  it('routes every dialog event to its own dialog', () => {
    const openDialog = vi.fn();
    const navigateToKnowledgeGraph = vi.fn();
    const commands = buildHomeToolbarCommands({ openDialog, navigateToKnowledgeGraph });

    for (const [event, dialog] of DIALOG_EVENTS) {
      openDialog.mockClear();
      navigateToKnowledgeGraph.mockClear();

      commands[event]?.();

      expect(openDialog).toHaveBeenCalledTimes(1);
      expect(openDialog).toHaveBeenCalledWith(dialog);
      expect(navigateToKnowledgeGraph).not.toHaveBeenCalled();
    }
  });

  it('routes knowledgeGraph to the route navigation instead of a dialog', () => {
    const openDialog = vi.fn();
    const navigateToKnowledgeGraph = vi.fn();
    const commands = buildHomeToolbarCommands({ openDialog, navigateToKnowledgeGraph });

    commands.knowledgeGraph?.();

    expect(navigateToKnowledgeGraph).toHaveBeenCalledTimes(1);
    expect(openDialog).not.toHaveBeenCalled();
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
