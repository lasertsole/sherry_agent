/**
 * Shell column floors shared by the layout and the two draggable columns.
 *
 * The conversation column has TWO floors: an absolute one (320px — below that a
 * message row is unreadable) and a proportional one (35% of the viewport, so on
 * a large display the chat keeps a share of the window instead of shrinking to
 * the absolute floor while the side panels eat the rest). CSS `max()` applies
 * both at once; `middleMinWidth()` is the same rule in pixels for the callers
 * that must reason about it (the right column's drag ceiling).
 */

/** Absolute floor of the conversation column, in px. */
export const MIDDLE_MIN_WIDTH_PX = 320;
/** Proportional floor of the conversation column (fraction of the viewport). */
export const MIDDLE_MIN_WIDTH_RATIO = 0.35;

/**
 * The conversation column's floor in px for a given viewport width.
 * @param viewportWidth Window width in px.
 * @returns `max(320, 35% of the viewport)`.
 */
export function middleMinWidth(viewportWidth: number): number {
  if (!Number.isFinite(viewportWidth) || viewportWidth <= 0) return MIDDLE_MIN_WIDTH_PX;
  return Math.max(MIDDLE_MIN_WIDTH_PX, Math.round(viewportWidth * MIDDLE_MIN_WIDTH_RATIO));
}

/**
 * The CSS expression for the conversation floor (`max(320px, 35%)`).
 * Percentages resolve against the flex row (the shell's full width).
 * @returns A CSS length accepted by `min-width`.
 */
export function middleMinWidthCss(): string {
  return `max(${MIDDLE_MIN_WIDTH_PX}px, ${MIDDLE_MIN_WIDTH_RATIO * 100}%)`;
}
