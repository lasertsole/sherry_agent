/**
 * Placement math for the toolbar's upward panels (`ToolbarPopover.vue`).
 *
 * The panel sits above its trigger and grows away from the nearer window edge,
 * the way the sibling controls do: a trigger on the left half aligns its left
 * edge with the trigger and grows right, one on the right half aligns its right
 * edge and grows left. Either way the result is clamped inside the viewport, so
 * a trigger at the very edge cannot push the panel out of the window.
 */

/** Gap kept between the panel and the viewport edges. */
export const TOOLBAR_POPOVER_EDGE_GAP = 8;

export interface ToolbarPopoverPlacement {
  /** Trigger's viewport rect, as the anchor. */
  triggerLeft: number;
  triggerRight: number;
  /** Panel width in px. */
  panelWidth: number;
  /** Viewport width in px. */
  viewportWidth: number;
}

/**
 * Left offset (viewport px) the panel should be placed at.
 * @param placement Anchor + panel geometry.
 */
export function anchorPanelLeft(placement: ToolbarPopoverPlacement): number {
  const { triggerLeft, triggerRight, panelWidth, viewportWidth } = placement;
  const gap = TOOLBAR_POPOVER_EDGE_GAP;
  const triggerCenter = triggerLeft + (triggerRight - triggerLeft) / 2;
  const anchored = triggerCenter < viewportWidth / 2 ? triggerLeft : triggerRight - panelWidth;
  const maxLeft = Math.max(viewportWidth - gap - panelWidth, gap);
  return Math.round(Math.min(Math.max(anchored, gap), maxLeft));
}
