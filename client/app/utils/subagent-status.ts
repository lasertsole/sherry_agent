/**
 * Subagent run status → presentation metadata.
 *
 * A run carries three status vocabularies (`execution.status`,
 * `delivery.status`, `execution.outcome.status`). Before this module the same
 * per-status values were spelled out in four places: `badgeClass` and
 * `statusLabel` (composables/useSubagentTasks.ts) plus `statusColorLight` /
 * `statusColorDark` and `statusKey` (SubagentFlowGraph.vue). This table is the
 * single source; the call sites keep their own priority chain (badge vs label
 * vs graph key differ) and only look values up here.
 *
 * Values are frozen presentation data — i18n keys and Tailwind class strings
 * must not change without the tests in `utils/__tests__/subagent-status.test.ts`
 * being updated deliberately.
 */

/** Status vocabulary of a run: execution status, delivery status and outcome status. */
export type SubagentStatus =
  'RUNNING' | 'INTERRUPTED' | 'PENDING' | 'IN_PROGRESS' | 'DELIVERED' | 'OK' | 'ERROR' | 'TIMEOUT' | 'KILLED';

/** Presentation of one status: label key, badge classes, node colors, graph key. */
export interface SubagentStatusMeta {
  /** i18n key under `sidebar.*` for the status label. */
  labelKey: string;
  /** Tailwind classes of the status badge. */
  badgeClass: string;
  /** Flow-graph node stroke color in light mode. */
  colorLight: string;
  /** Flow-graph node stroke color in dark mode. */
  colorDark: string;
  /** Flow-graph node status key (`running` / `completed` / `failed` / `unknown`). */
  flowKey: string;
}

/** Neutral badge shared by every status without a color of its own. */
const NEUTRAL_BADGE = 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300';

/** Neutral flow-graph node color (light mode). */
const NEUTRAL_COLOR_LIGHT = '#64748b';

/** Neutral flow-graph node color (dark mode). */
const NEUTRAL_COLOR_DARK = '#94a3b8';

/**
 * Presentation metadata per status.
 *
 * Delivery statuses (`PENDING` / `IN_PROGRESS` / `DELIVERED`) only feed the
 * label; their badge/color fields carry the neutral presentation so the table
 * stays total.
 */
export const SUBAGENT_STATUS_META: Record<SubagentStatus, SubagentStatusMeta> = {
  RUNNING: {
    labelKey: 'sidebar.statusRunning',
    badgeClass: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
    colorLight: '#3b82f6',
    colorDark: '#60a5fa',
    flowKey: 'running'
  },
  INTERRUPTED: {
    labelKey: 'sidebar.statusInterrupted',
    badgeClass: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
    colorLight: '#3b82f6',
    colorDark: '#60a5fa',
    flowKey: 'running'
  },
  PENDING: {
    labelKey: 'sidebar.statusPending',
    badgeClass: NEUTRAL_BADGE,
    colorLight: NEUTRAL_COLOR_LIGHT,
    colorDark: NEUTRAL_COLOR_DARK,
    flowKey: 'unknown'
  },
  IN_PROGRESS: {
    labelKey: 'sidebar.statusInProgress',
    badgeClass: NEUTRAL_BADGE,
    colorLight: NEUTRAL_COLOR_LIGHT,
    colorDark: NEUTRAL_COLOR_DARK,
    flowKey: 'unknown'
  },
  DELIVERED: {
    labelKey: 'sidebar.statusDelivered',
    badgeClass: NEUTRAL_BADGE,
    colorLight: NEUTRAL_COLOR_LIGHT,
    colorDark: NEUTRAL_COLOR_DARK,
    flowKey: 'unknown'
  },
  OK: {
    labelKey: 'sidebar.statusDone',
    badgeClass: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
    colorLight: '#10b981',
    colorDark: '#34d399',
    flowKey: 'completed'
  },
  ERROR: {
    labelKey: 'sidebar.statusError',
    badgeClass: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
    colorLight: '#ef4444',
    colorDark: '#f87171',
    flowKey: 'failed'
  },
  TIMEOUT: {
    labelKey: 'sidebar.statusTimeout',
    badgeClass: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
    colorLight: '#ef4444',
    colorDark: '#f87171',
    flowKey: 'failed'
  },
  KILLED: {
    labelKey: 'sidebar.statusKilled',
    badgeClass: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
    colorLight: '#ef4444',
    colorDark: '#f87171',
    flowKey: 'failed'
  }
};

/** Presentation of a status the backend did not enumerate (unknown / missing). */
export const SUBAGENT_STATUS_FALLBACK: SubagentStatusMeta = {
  labelKey: 'sidebar.statusUnknown',
  badgeClass: NEUTRAL_BADGE,
  colorLight: NEUTRAL_COLOR_LIGHT,
  colorDark: NEUTRAL_COLOR_DARK,
  flowKey: 'unknown'
};

/**
 * Look up the presentation of a raw status string.
 * Unknown / missing statuses resolve to {@link SUBAGENT_STATUS_FALLBACK}.
 * @param status Raw status value off a run record (may be absent).
 * @returns The matching metadata, or the fallback entry.
 */
export function subagentStatusMeta(status: string | null | undefined): SubagentStatusMeta {
  if (!status) return SUBAGENT_STATUS_FALLBACK;
  return SUBAGENT_STATUS_META[status as SubagentStatus] ?? SUBAGENT_STATUS_FALLBACK;
}

/**
 * Flow-graph node stroke color for a raw status in light mode.
 * @param status Raw status value off a run record (may be absent).
 * @returns A hex color.
 */
export function subagentStatusColorLight(status: string | null | undefined): string {
  return subagentStatusMeta(status).colorLight;
}

/**
 * Flow-graph node stroke color for a raw status in dark mode.
 * @param status Raw status value off a run record (may be absent).
 * @returns A hex color.
 */
export function subagentStatusColorDark(status: string | null | undefined): string {
  return subagentStatusMeta(status).colorDark;
}
