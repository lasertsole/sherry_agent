/**
 * Windowed listing for the left sidebar's two lists (sessions / background
 * tasks), built on `@tanstack/vue-virtual`.
 *
 * Why these two lists need it: the sidebar is a fixed-height column, and both
 * lists are unbounded in practice — a long-lived install accumulates hundreds
 * of sessions, and the subagent registry accumulates thousands of task runs
 * (7484 first-level runs in the dev database at the time of writing). Rendering
 * one card per entry made every sidebar interaction pay for the whole list.
 *
 * Differences from `use-chat-virtual-list` (deliberately NOT shared):
 *  - top-anchored, no pagination, nothing to preserve across prepends — the
 *    chat list is bottom-anchored with older-history prepends;
 *  - no follow-the-tail semantics, no KeepAlive position restore;
 *  - the container is a plain fixed-height box, so the only lifecycle hook
 *    needed is a re-measure when the tab (or the cached page) re-attaches.
 *
 * `measureElement` still drives dynamic heights: a session row grows while its
 * title is renamed inline, and task cards wrap at two lines.
 *
 * @module composables/use-sidebar-virtual-rows
 */
import { onActivated, nextTick } from 'vue';
import { useVirtualizer } from '@tanstack/vue-virtual';
import type { SessionRecord } from '~/pages/home/type';
import type { SubagentRun } from '~/composables/bridge';
import type { TaskSessionGroup } from '~/utils/subagent';

/** Rows kept mounted outside the viewport (both directions). */
const OVERSCAN = 6;

/** First-render height guesses, corrected by real measurement. */
export const SESSION_ROW_ESTIMATE_PX = 100;
export const TASK_HEADER_ESTIMATE_PX = 28;
export const TASK_RUN_ESTIMATE_PX = 96;

/** One windowed row: a stable identity key plus its model. */
export interface SidebarVirtualRow {
  /**
   * Stable row key. Rows are matched by key (not index) so a filtered or
   * re-fetched list re-measures only what actually changed.
   */
  key: string;
}

/** A session row: one history entry. */
export interface SessionVirtualRow extends SidebarVirtualRow {
  item: SessionRecord;
}

/** A task row: either a calling-session header or one run card. */
export interface TaskVirtualRow extends SidebarVirtualRow {
  kind: 'header' | 'run';
  /** Calling session this row belongs to (header text / grouping key). */
  sessionId: string;
  /** Run count shown on a header row. */
  runCount: number;
  /** Present on `kind === 'run'` rows. */
  run?: SubagentRun;
}

/**
 * Flatten the session list into virtual rows.
 * @param list Sessions in display order.
 * @returns One row per session, keyed by session id.
 */
export const buildSessionRows = (list: SessionRecord[]): SessionVirtualRow[] =>
  list.map(item => ({ key: `s-${item.id}`, item }));

/**
 * Flatten the grouped task runs into virtual rows: a header row followed by its
 * run cards. Flattening (rather than virtualizing each group separately) keeps
 * a single scroll geometry for the whole tab.
 * @param groups Task groups in display order.
 * @returns Header + run rows, keyed by session id and run id.
 */
export const buildTaskRows = (groups: TaskSessionGroup[]): TaskVirtualRow[] =>
  groups.flatMap(group => [
    {
      key: `h-${group.sessionId}`,
      kind: 'header' as const,
      sessionId: group.sessionId,
      runCount: group.runs.length
    },
    ...group.runs.map(run => ({
      key: `r-${run.run_id}`,
      kind: 'run' as const,
      sessionId: group.sessionId,
      runCount: group.runs.length,
      run
    }))
  ]);

/**
 * Estimated height of a task row (headers are one text line, cards are not).
 * @param rows Row models the estimate is read from.
 * @param index Row index being estimated.
 * @returns The height guess in px.
 */
export const taskRowEstimate = (rows: TaskVirtualRow[], index: number): number =>
  rows[index]?.kind === 'header' ? TASK_HEADER_ESTIMATE_PX : TASK_RUN_ESTIMATE_PX;

/**
 * Create a windowed controller over `rows` for the scroll container `scrollRef`.
 *
 * The caller owns the DOM: a spacers div of `totalSize` height holds absolutely
 * positioned rows translated by `start` (see `SessionSidebar.vue`), and binds
 * `:ref="el => virtualizer.measureElement(el)"` on every row so dynamic heights
 * feed back into the geometry.
 *
 * @param scrollRef Scroll container ref (resolved after mount; may be null while
 *   the owning tab is hidden).
 * @param rows Getter returning the current row models.
 * @param estimateSize Height guess per row index, corrected by measurement.
 * @returns The virtualizer, the current window, the spacer height, a row lookup,
 *   and the re-measure hook.
 */
export function useVirtualRows<T extends SidebarVirtualRow>(
  scrollRef: Ref<HTMLElement | null | undefined>,
  rows: () => T[],
  estimateSize: (index: number) => number
) {
  const virtualizer = useVirtualizer({
    // Getters keep the options reactive: the adapter re-applies them whenever a
    // tracked value changes (count / keys / scroll element).
    get count() {
      return rows().length;
    },
    getScrollElement: () => scrollRef.value ?? null,
    estimateSize,
    getItemKey: (index: number) => rows()[index]?.key ?? String(index),
    overscan: OVERSCAN
  });

  /** Positioned rows currently in the window. */
  const virtualRows = computed(() => virtualizer.value.getVirtualItems());
  /** Total scrollable height of all rows. */
  const totalSize = computed(() => virtualizer.value.getTotalSize());

  /**
   * Row model by virtual index (the template holds indexes, this holds models).
   * @param index
   * @returns The row model at that index, or undefined past the end.
   */
  const rowAt = (index: number): T | undefined => rows()[index];

  /**
   * Re-measure after the container appears or its box changes (tab switch, a
   * KeepAlive re-attach). Without it the first window is computed against a
   * stale/zero viewport and the list can render empty until the first scroll.
   */
  const remeasure = () => {
    void nextTick(() => virtualizer.value.measure());
  };

  // Outside KeepAlive this never fires; inside it, the cached page is re-attached
  // with its DOM intact but the viewport size possibly changed.
  onActivated(remeasure);

  return { virtualizer, virtualRows, totalSize, rowAt, remeasure };
}
