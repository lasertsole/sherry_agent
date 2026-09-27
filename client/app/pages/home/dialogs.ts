/**
 * Home-shell dialog registry + toolbar command registry.
 *
 * `HOME_DIALOG_IDS` lists every dialog the shell owns (fed to
 * `useDialogManager`), and `HOME_TOOLBAR_EVENTS` + `buildHomeToolbarCommands`
 * map a toolbar event to the command it runs.
 *
 * ``logs`` / ``stats`` / ``knowledgeGraph`` are not dialogs: their commands
 * open a tab in the collapsible right sidebar, so a viewer stays open next to
 * the chat.
 */

/** Dialog ids owned by the home shell. */
export const HOME_DIALOG_IDS = [
  'skills',
  'systemConfig',
  'persona',
  'memory',
  'heartbeat',
  'cron',
  'notification',
  'extend'
] as const;

/** One dialog id of the home shell. */
export type HomeDialogId = (typeof HOME_DIALOG_IDS)[number];

/**
 * Toolbar event vocabulary: every `headerTools` entry (the nine-grid, see
 * ./config.ts) plus the two top-bar-only buttons (`logs`, `notification`).
 * The unit test pins this list against `headerTools` so a new tool entry
 * without a command fails there.
 */
export const HOME_TOOLBAR_EVENTS = [
  'skills',
  'knowledgeGraph',
  'stats',
  'systemConfig',
  'persona',
  'memory',
  'heartbeat',
  'cron',
  'logs',
  'notification',
  'extend'
] as const;

/** One toolbar event of the home shell. */
export type HomeToolbarEvent = (typeof HOME_TOOLBAR_EVENTS)[number];

/** What a toolbar command may do: open a dialog or open a right-sidebar tab. */
export interface HomeToolbarContext {
  /** Open the given dialog (from `useDialogManager`). */
  openDialog: (id: HomeDialogId) => void;
  /** Open a right-sidebar tab: the log viewer, the statistics charts, the knowledge graph. */
  openRightTab: (kind: 'logs' | 'stats' | 'knowledgeGraph') => void;
}

/**
 * Build the toolbar command registry. Every event maps to exactly one command;
 * the internal `Record<HomeToolbarEvent, …>` makes a missing entry a compile
 * error, and the returned index-signature type keeps the dispatcher assertion
 * free for unknown events.
 * @param context Shell actions the commands delegate to.
 * @returns Event → command table (unknown events resolve to `undefined`).
 */
export function buildHomeToolbarCommands(context: HomeToolbarContext): Record<string, () => void> {
  const commands: Record<HomeToolbarEvent, () => void> = {
    skills: () => context.openDialog('skills'),
    knowledgeGraph: () => context.openRightTab('knowledgeGraph'),
    stats: () => context.openRightTab('stats'),
    systemConfig: () => context.openDialog('systemConfig'),
    persona: () => context.openDialog('persona'),
    memory: () => context.openDialog('memory'),
    heartbeat: () => context.openDialog('heartbeat'),
    cron: () => context.openDialog('cron'),
    logs: () => context.openRightTab('logs'),
    notification: () => context.openDialog('notification'),
    extend: () => context.openDialog('extend')
  };
  return commands;
}
