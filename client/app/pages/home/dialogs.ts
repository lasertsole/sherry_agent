import type { RightSidebarPanelKind } from '~/stores/right-sidebar';

/**
 * Home-shell dialog registry + toolbar command registry.
 *
 * `HOME_DIALOG_IDS` lists every dialog the shell still owns (fed to
 * `useDialogManager`) and `HOME_TOOLBAR_EVENTS` + `buildHomeToolbarCommands`
 * map a toolbar event to the command it runs.
 *
 * NO dialog is left: every entry — the log viewer, the statistics charts, the
 * knowledge graph, the notification list and the skill / system-config /
 * persona / memory / heartbeat / cron / extend editors — opens a tab in the
 * collapsible right sidebar, so a tool stays open next to the chat. The registry
 * stays (empty) so a future dialog is one id here plus one mount in the shell;
 * the notification store keeps the badge live while its tab is closed.
 */

/** Dialog ids owned by the home shell (currently none). */
export const HOME_DIALOG_IDS = [] as const;

/** One dialog id of the home shell. */
export type HomeDialogId = (typeof HOME_DIALOG_IDS)[number];

/**
 * Toolbar event vocabulary: every `headerTools` entry (the nine-grid, see
 * ./config.ts) plus the one top-bar-only button (`notification`). The unit test
 * pins this list against `headerTools` so a new tool entry without a command
 * fails there.
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
  'extend',
  'account'
] as const;

/** One toolbar event of the home shell. */
export type HomeToolbarEvent = (typeof HOME_TOOLBAR_EVENTS)[number];

/** What a toolbar command may do: open a right-sidebar tab. */
export interface HomeToolbarContext {
  /**
   * Open a right-sidebar tab for the given panel kind. (A `openDialog` member
   * belongs here again the day `HOME_DIALOG_IDS` gains an entry; no command
   * uses one today.)
   */
  openRightTab: (kind: RightSidebarPanelKind) => void;
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
    skills: () => context.openRightTab('skills'),
    knowledgeGraph: () => context.openRightTab('knowledgeGraph'),
    stats: () => context.openRightTab('stats'),
    systemConfig: () => context.openRightTab('systemConfig'),
    persona: () => context.openRightTab('persona'),
    memory: () => context.openRightTab('memory'),
    heartbeat: () => context.openRightTab('heartbeat'),
    cron: () => context.openRightTab('cron'),
    logs: () => context.openRightTab('logs'),
    notification: () => context.openRightTab('notification'),
    extend: () => context.openRightTab('extend'),
    account: () => context.openRightTab('account')
  };
  return commands;
}
