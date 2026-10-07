import type { RightSidebarPanelKind } from '~/stores/right-sidebar';

/**
 * Home-shell toolbar command registry.
 *
 * `HOME_TOOLBAR_EVENTS` + `buildHomeToolbarCommands` map a toolbar event to the
 * command it runs. Every entry — the log viewer, the statistics charts, the
 * knowledge graph, the notification list and the skill / system-config /
 * persona / memory / heartbeat / cron / extend editors — opens a tab in the
 * collapsible right sidebar, so a tool stays open next to the chat (the
 * notification store keeps its badge live while that tab is closed).
 */

/**
 * Toolbar event vocabulary: every `headerTools` entry (the nine-grid, see
 * ./config.ts), the two `toolboxTools` entries (the hammer dialog) and the one
 * top-bar-only button (`notification`). The unit test pins this list against both
 * grids so a new entry without a command fails there.
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
  'account',
  // The toolbox (./config.ts `toolboxTools`): the browser and the user terminal.
  'browser',
  'terminal'
] as const;

/** One toolbar event of the home shell. */
export type HomeToolbarEvent = (typeof HOME_TOOLBAR_EVENTS)[number];

/** What a toolbar command may do: open a right-sidebar tab. */
export interface HomeToolbarContext {
  /**
   * Open a right-sidebar tab for the given panel kind.
   * @param kind Panel to open.
   * @param payload Optional identity.
   * @param payload.instance An instance key makes the tab a TWIN (the toolbox's
   *   browser / terminal open one per click) instead of activating an existing
   *   tab of that kind.
   */
  openRightTab: (kind: RightSidebarPanelKind, payload?: { instance?: string }) => void;
}

/** Monotonic counter behind the toolbox's per-click instance keys. */
let toolboxInstanceSeq = 0;

/**
 * A fresh instance key for one toolbox panel (per page load — the tabs
 * themselves are in-memory too).
 * @param kind
 */
function nextToolboxInstance(kind: string): string {
  toolboxInstanceSeq += 1;
  return `${kind}#${toolboxInstanceSeq}`;
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
    account: () => context.openRightTab('account'),
    // The toolbox panels open a NEW tab per click: several browsers / terminals
    // side by side is the point (each keeps its own page / scrollback).
    browser: () => context.openRightTab('browser', { instance: nextToolboxInstance('browser') }),
    terminal: () => context.openRightTab('terminal', { instance: nextToolboxInstance('terminal') })
  };
  return commands;
}
