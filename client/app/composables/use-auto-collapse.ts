import { onBeforeUnmount, onMounted } from 'vue';

/**
 * Auto-collapse the side columns when the window gets too narrow to hold a
 * usable conversation (ZCode's workspace-shell safety net).
 *
 * The shell is `left sidebar + conversation + right sidebar` in one flex row.
 * Once the conversation is squeezed below a readable width, the extra columns
 * are worth less than the chat, so they retract — right column first (it is
 * the accessory), the left one second (it is the navigation).
 *
 * Only WINDOW RESIZES trigger this. Opening a panel by hand never collapses
 * another one: a deliberate click must not be undone by the layout, and the
 * thresholds would otherwise fight the user every time they widened something.
 */

/** Below this conversation width the right column retracts. */
export const COLLAPSE_SIDE_PANE_PX = 480;
/** Below this conversation width the left column retracts (measured after the right one is gone). */
export const COLLAPSE_SIDEBAR_PX = 360;
/** Resize events are debounced by this much (a drag-resize fires per frame). */
export const RESIZE_IDLE_MS = 300;

/**
 * Width the conversation column has left.
 * @param viewportWidth Window width in px.
 * @param left Already-retracted left column contributes 0.
 * @param left.collapsed
 * @param left.width
 * @param right Already-retracted right column contributes 0.
 * @param right.collapsed
 * @param right.width
 * @returns Pixels left for the conversation column.
 */
export function conversationWidth(
  viewportWidth: number,
  left: { collapsed: boolean; width: number },
  right: { collapsed: boolean; width: number }
): number {
  return viewportWidth - (left.collapsed ? 0 : left.width) - (right.collapsed ? 0 : right.width);
}

/**
 * The collapse this layout size calls for, or `null` when everything fits.
 *
 * Both decisions are made against the SAME measurement, right column first:
 * on a window narrow enough to trip both, the right one collapses and the
 * left one is left alone (its own re-measure happens after the right panel
 * has actually unmounted — see the composable).
 * @param viewportWidth Window width in px.
 * @param left Left column state.
 * @returns The column to retract now, or ``null``.
 * @param left.collapsed
 * @param left.width
 * @param right Right column state.
 * @param right.collapsed
 * @param right.width
 */
export function autoCollapseTarget(
  viewportWidth: number,
  left: { collapsed: boolean; width: number },
  right: { collapsed: boolean; width: number }
): 'right' | null {
  const width = conversationWidth(viewportWidth, left, right);
  if (width < COLLAPSE_SIDE_PANE_PX && !right.collapsed) return 'right';
  return null;
}

/**
 * Whether the left column should retract, measured WITHOUT the right column
 * (it is either already collapsed or about to be).
 * @param viewportWidth Window width in px.
 * @param left Left column state.
 * @param left.collapsed
 * @param left.width
 * @returns Whether the left column should retract.
 */
export function shouldCollapseLeft(viewportWidth: number, left: { collapsed: boolean; width: number }): boolean {
  if (left.collapsed) return false;
  return viewportWidth - left.width < COLLAPSE_SIDEBAR_PX;
}

/**
 * The two columns the composable watches and may retract.
 *
 * Accessors, not refs: the composable both reads and WRITES the two collapse
 * flags, and a `computed(() => store.collapsed)` would throw on write (Vue only
 * warns, so the collapse would silently never happen). Getters/setters also
 * keep the composable honest against the test doubles, whose stores are plain
 * reactive objects rather than writable refs.
 */
export interface AutoCollapseDeps {
  /** Left column collapsed? */
  isSidebarCollapsed: () => boolean;
  /** Retract or expand the left column. */
  setSidebarCollapsed: (collapsed: boolean) => void;
  /** Left column expanded width in px. */
  sidebarWidth: () => number;
  /** Right column collapsed? */
  isRightCollapsed: () => boolean;
  /** Retract or expand the right column. */
  setRightCollapsed: (collapsed: boolean) => void;
  /** Right column width in px. */
  rightSidebarWidth: () => number;
  /**
   * Pause the net while the conversation column is floated: the middle column
   * then takes no flex width, so "viewport − side columns" no longer describes
   * the conversation's width and measuring would retract the side columns the
   * user just widened on any window resize.
   */
  isSuspended?: () => boolean;
}

/**
 * Attach the auto-collapse behaviour to a component.
 *
 * The left check runs two animation frames after the right column collapsed,
 * so it measures the layout the user will actually get rather than the one
 * with a panel that is still animating out.
 * @param deps The two columns' refs.
 * @returns A `measure` hook: call it after the floating column is docked (the
 *   window-resize path is suspended while it floats, so the restored layout is
 *   not evaluated until the caller asks).
 */
export function useAutoCollapse(deps: AutoCollapseDeps): { measure: () => void } {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let frame: number | undefined;

  const leftState = () => ({
    collapsed: deps.isSidebarCollapsed(),
    width: deps.sidebarWidth()
  });

  const measure = (): void => {
    if (deps.isSuspended?.()) return;
    const viewport = window.innerWidth;
    const right = {
      collapsed: deps.isRightCollapsed(),
      width: deps.rightSidebarWidth()
    };
    if (autoCollapseTarget(viewport, leftState(), right) === 'right') {
      deps.setRightCollapsed(true);
      if (frame !== undefined) cancelAnimationFrame(frame);
      // Two frames: the first paints the now-single-column layout, the second
      // measures it — the left decision must see the panel actually gone.
      frame = requestAnimationFrame(() => {
        frame = requestAnimationFrame(() => {
          if (shouldCollapseLeft(viewport, leftState())) deps.setSidebarCollapsed(true);
        });
      });
      return;
    }
    if (shouldCollapseLeft(viewport, leftState())) deps.setSidebarCollapsed(true);
  };

  const onResize = (): void => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(measure, RESIZE_IDLE_MS);
  };

  // Also check ONCE at mount: a layout restored from localStorage (a wide left
  // column plus an open right panel) can already be too narrow for the
  // conversation, and waiting for the first user resize would leave it that way.
  onMounted(() => {
    window.addEventListener('resize', onResize);
    measure();
  });
  onBeforeUnmount(() => {
    window.removeEventListener('resize', onResize);
    if (timer) clearTimeout(timer);
    if (frame !== undefined) cancelAnimationFrame(frame);
  });

  return { measure };
}
