/**
 * Home-shell dialog registry + toolbar command registry.
 *
 * The shell used to dispatch its toolbar with an 11-case `switch` over
 * `handleOperate` while each dialog had its own `showXxxDialog` ref. Both are
 * now data: `HOME_DIALOG_IDS` lists every dialog the shell owns (fed to
 * `useDialogManager`), and `HOME_TOOLBAR_EVENTS` + `buildHomeToolbarCommands`
 * map a toolbar event to the command it runs.
 */

/** Dialog ids owned by the home shell. */
export const HOME_DIALOG_IDS = [
  'skills',
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

/** What a toolbar command may do: open a dialog or navigate. */
export interface HomeToolbarContext {
  /** Open the given dialog (from `useDialogManager`). */
  openDialog: (id: HomeDialogId) => void;
  /** Navigate to the standalone knowledge-graph page. */
  navigateToKnowledgeGraph: () => void;
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
    knowledgeGraph: () => context.navigateToKnowledgeGraph(),
    stats: () => context.openDialog('stats'),
    systemConfig: () => context.openDialog('systemConfig'),
    persona: () => context.openDialog('persona'),
    memory: () => context.openDialog('memory'),
    heartbeat: () => context.openDialog('heartbeat'),
    cron: () => context.openDialog('cron'),
    logs: () => context.openDialog('logs'),
    notification: () => context.openDialog('notification'),
    extend: () => context.openDialog('extend')
  };
  return commands;
}
