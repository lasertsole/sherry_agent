import type { CommandEntry } from '~/composables/use-command-registry';
import { headerTools, toolboxTools } from './config';
import { buildHomeToolbarCommands, type HomeToolbarContext } from './dialogs';

/**
 * The palette's STATIC commands: everything the 菜单 grid and the toolbox can
 * open, plus "New chat".
 *
 * The grid is the source of truth for labels, icons and events — the palette
 * builds its rows from the SAME arrays the dialog renders, so a new tile shows
 * up in both without a second table. `buildHomeToolbarCommands` supplies the
 * handler, and `t('toolbar.<event>')` the label; the notification bell is the
 * one command that is not a tile (it lives in the top bar).
 */

/** Events that read as navigation rather than settings (the toolbox surfaces). */
const NAVIGATE_EVENTS = new Set(['browser', 'terminal']);

/**
 * One command per grid / toolbox entry.
 * @param context Shell actions (`openRightTab`).
 * @param t Translator.
 * @returns Palette entries in grid order, toolbox after the grid.
 */
export function buildToolbarCommandEntries(context: HomeToolbarContext, t: (key: string) => string): CommandEntry[] {
  const commands = buildHomeToolbarCommands(context);
  return [...headerTools, ...toolboxTools].map(tool => ({
    id: `toolbar.${tool.event}`,
    label: t(tool.toolName),
    section: NAVIGATE_EVENTS.has(tool.event) ? 'navigate' : 'tools',
    icon: tool.icon,
    keywords: [tool.event, tool.title ?? tool.toolName],
    handler: commands[tool.event] ?? (() => {})
  }));
}

/**
 * The notification list (a top-bar button, not a grid tile).
 * @param context Shell actions.
 * @param t Translator.
 */
export function buildNotificationCommandEntries(
  context: HomeToolbarContext,
  t: (key: string) => string
): CommandEntry[] {
  const commands = buildHomeToolbarCommands(context);
  return [
    {
      id: 'toolbar.notification',
      label: t('toolbar.notification'),
      section: 'navigate',
      icon: 'pi pi-bell',
      keywords: ['notification', 'bell'],
      handler: commands.notification ?? (() => {})
    }
  ];
}

/**
 * "New chat" — the preset dialog, the same entry the toolbar's plus button runs.
 * @param openDialog The new-session store's opener.
 * @param t Translator.
 */
export function buildSessionCommandEntries(openDialog: () => void, t: (key: string) => string): CommandEntry[] {
  return [
    {
      id: 'session.new',
      label: t('commandPalette.newSession'),
      section: 'session',
      icon: 'pi pi-plus',
      keywords: ['new', 'create', '新建', '新規', '새로'],
      handler: openDialog
    }
  ];
}
