/**
 * Geometry for the floating conversation column (the middle column popped out
 * of the shell's flex row — ZCode's side-pane full-view float, applied to the
 * chat column instead of a side pane).
 *
 * Pure math only: no DOM, no storage. The page measures the viewport once and
 * feeds a rect in here; every anchor, clamp and size is derived from it, so the
 * floating position can be unit-tested without a browser.
 *
 * @module common/floating-window
 */

/** One of the nine snap positions a released float settles into. */
export type FloatingAnchor =
  | 'top-left'
  | 'top-center'
  | 'top-right'
  | 'left-center'
  | 'center'
  | 'right-center'
  | 'bottom-left'
  | 'bottom-center'
  | 'bottom-right';

/** A viewport-sized box the float lives inside (the shell's content area). */
export interface FloatingRect {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

/** A float's box size. */
export interface FloatingSize {
  readonly width: number;
  readonly height: number;
}

/** A float's box position (viewport coordinates). */
export interface FloatingPosition {
  readonly left: number;
  readonly top: number;
}

/** The freed-width share each expanded side column receives. */
export interface SideExtraWidth {
  readonly left: number;
  readonly right: number;
}

/** Every snap position, in the order `resolveNearestFloatingAnchor` breaks ties. */
export const FLOATING_ANCHORS: readonly FloatingAnchor[] = [
  'top-left',
  'top-center',
  'top-right',
  'left-center',
  'center',
  'right-center',
  'bottom-left',
  'bottom-center',
  'bottom-right'
];

/** Where a float appears before the user has ever dragged it: the old column's place. */
export const DEFAULT_FLOATING_ANCHOR: FloatingAnchor = 'center';

/** Gap kept between the float and the slot's edges. */
export const FLOATING_MARGIN_PX = 16;

/** Preferred width as a share of the slot. */
export const FLOATING_WIDTH_RATIO = 0.45;
/** Narrowest usable conversation window. */
export const FLOATING_WIDTH_MIN_PX = 480;
/** Widest the float may get (a small window, not a second shell). */
export const FLOATING_WIDTH_MAX_PX = 760;

/** Preferred height as a share of the slot. */
export const FLOATING_HEIGHT_RATIO = 0.78;
/** Shortest window that still fits the header, toolbar, composer and messages. */
export const FLOATING_HEIGHT_MIN_PX = 420;
/** Tallest the float may get. */
export const FLOATING_HEIGHT_MAX_PX = 820;

/** Pointer travel below this is a click, not a drag (ZCode's threshold). */
export const FLOATING_DRAG_CLICK_THRESHOLD_PX = 4;

/** Below this viewport width the float is not offered (it would cover the shell). */
export const FLOATING_MIN_VIEWPORT_WIDTH_PX = 640;

/** Narrowest the user may shrink the window to by dragging. */
export const FLOATING_RESIZE_WIDTH_MIN_PX = 360;
/** Widest the user may grow the window to by dragging. */
export const FLOATING_RESIZE_WIDTH_MAX_PX = 1200;
/** Shortest EXPANDED window; dragging below this snaps into the input-only bar. */
export const FLOATING_RESIZE_HEIGHT_MIN_PX = 260;
/** Tallest the user may grow the window to by dragging. */
export const FLOATING_RESIZE_HEIGHT_MAX_PX = 900;

/** A size the caller wants instead of the ratio default (0 / absent = "use the default"). */
export interface FloatingPreferredSize {
  readonly width?: number;
  readonly height?: number;
}

/**
 * Adopt a user-chosen size inside the drag-resize band and the slot.
 * @param size Requested size in px.
 * @param slot The shell's content rect.
 * @returns The size clamped to 360–1200 × 260–900 and 16px inside the slot.
 */
export function clampFloatingSize(size: FloatingSize, slot: FloatingRect): FloatingSize {
  const maxWidth = Math.max(0, Math.min(FLOATING_RESIZE_WIDTH_MAX_PX, slot.width - FLOATING_MARGIN_PX * 2));
  const maxHeight = Math.max(0, Math.min(FLOATING_RESIZE_HEIGHT_MAX_PX, slot.height - FLOATING_MARGIN_PX * 2));
  const width = Math.max(FLOATING_RESIZE_WIDTH_MIN_PX, Math.round(size.width));
  const height = Math.max(FLOATING_RESIZE_HEIGHT_MIN_PX, Math.round(size.height));
  return {
    width: Math.min(width, maxWidth),
    height: Math.min(height, maxHeight)
  };
}

/**
 * The input-only (collapsed) bar's height: the composer dock's measured height,
 * capped by the slot. The dock is intrinsic, so no minimum is applied here.
 * @param dockHeight Measured height of the composer dock in px.
 * @param slot The shell's content rect.
 * @returns The bar height.
 */
export function clampCollapsedHeight(dockHeight: number, slot: FloatingRect): number {
  const maxHeight = Math.max(0, Math.min(FLOATING_RESIZE_HEIGHT_MAX_PX, slot.height - FLOATING_MARGIN_PX * 2));
  return Math.max(0, Math.min(Math.max(0, Math.round(dockHeight)), maxHeight));
}

/**
 * The float's size for a slot: 45% × 78% of the slot, kept inside the min/max
 * band and always 16px clear of the edges. A slot narrower than the band wins
 * over the minimum (a float wider than the window helps nobody). A preferred
 * (user-resized) dimension replaces the ratio for that axis and uses the wider
 * drag-resize band.
 * @param slot The shell's content rect.
 * @param preferred User-chosen size; 0 / absent axes keep the ratio default.
 * @returns The float's width and height in px.
 */
export function resolveFloatingSize(slot: FloatingRect, preferred: FloatingPreferredSize = {}): FloatingSize {
  const preferredWidth = preferred.width ?? 0;
  const preferredHeight = preferred.height ?? 0;
  if (preferredWidth > 0 || preferredHeight > 0) {
    return clampFloatingSize(
      {
        width: preferredWidth > 0 ? preferredWidth : defaultFloatingWidth(slot),
        height: preferredHeight > 0 ? preferredHeight : defaultFloatingHeight(slot)
      },
      slot
    );
  }
  return {
    width: defaultFloatingWidth(slot),
    height: defaultFloatingHeight(slot)
  };
}

/**
 * The ratio default for the width axis (clamped to the small-window band).
 * @param slot The shell's content rect.
 * @returns Default width in px.
 */
function defaultFloatingWidth(slot: FloatingRect): number {
  const maxWidth = Math.max(0, Math.min(FLOATING_WIDTH_MAX_PX, slot.width - FLOATING_MARGIN_PX * 2));
  return Math.min(Math.max(FLOATING_WIDTH_MIN_PX, Math.round(slot.width * FLOATING_WIDTH_RATIO)), maxWidth);
}

/**
 * The ratio default for the height axis (clamped to the small-window band).
 * @param slot The shell's content rect.
 * @returns Default height in px.
 */
function defaultFloatingHeight(slot: FloatingRect): number {
  const maxHeight = Math.max(0, Math.min(FLOATING_HEIGHT_MAX_PX, slot.height - FLOATING_MARGIN_PX * 2));
  return Math.min(Math.max(FLOATING_HEIGHT_MIN_PX, Math.round(slot.height * FLOATING_HEIGHT_RATIO)), maxHeight);
}

/**
 * Keep a dragged position inside the slot.
 * @param position Requested top-left corner.
 * @param slot The shell's content rect.
 * @param size The float's size.
 * @returns The position clamped into `[slot.left, slot.right - width]` × `[slot.top, slot.bottom - height]`.
 */
export function clampFloatingPosition(
  position: FloatingPosition,
  slot: FloatingRect,
  size: FloatingSize
): FloatingPosition {
  const maxLeft = slot.left + Math.max(0, slot.width - size.width);
  const maxTop = slot.top + Math.max(0, slot.height - size.height);
  return {
    left: Math.max(slot.left, Math.min(position.left, maxLeft)),
    top: Math.max(slot.top, Math.min(position.top, maxTop))
  };
}

/**
 * The resting top-left corner of one anchor (16px off the edges).
 * @param anchor Snap position.
 * @param slot The shell's content rect.
 * @param size The float's size.
 * @returns The corner the float sits at when settled on that anchor.
 */
export function resolveFloatingPosition(
  anchor: FloatingAnchor,
  slot: FloatingRect,
  size: FloatingSize
): FloatingPosition {
  const margin = FLOATING_MARGIN_PX;
  const horizontal = anchor.includes('left') ? 'start' : anchor.includes('right') ? 'end' : 'center';
  const vertical = anchor.startsWith('top') ? 'start' : anchor.startsWith('bottom') ? 'end' : 'center';
  const left =
    horizontal === 'start'
      ? slot.left + margin
      : horizontal === 'end'
        ? slot.left + slot.width - margin - size.width
        : slot.left + (slot.width - size.width) / 2;
  const top =
    vertical === 'start'
      ? slot.top + margin
      : vertical === 'end'
        ? slot.top + slot.height - margin - size.height
        : slot.top + (slot.height - size.height) / 2;
  return clampFloatingPosition({ left, top }, slot, size);
}

/**
 * The anchor whose resting corner is closest to the released position (ties go
 * to the earlier entry in `FLOATING_ANCHORS`).
 * @param position The float's top-left corner when the pointer was released.
 * @param slot The shell's content rect.
 * @param size The float's size.
 * @returns The nearest snap position.
 */
export function resolveNearestFloatingAnchor(
  position: FloatingPosition,
  slot: FloatingRect,
  size: FloatingSize
): FloatingAnchor {
  let best: FloatingAnchor = DEFAULT_FLOATING_ANCHOR;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const anchor of FLOATING_ANCHORS) {
    const resting = resolveFloatingPosition(anchor, slot, size);
    const distance = Math.hypot(resting.left - position.left, resting.top - position.top);
    if (distance < bestDistance) {
      best = anchor;
      bestDistance = distance;
    }
  }
  return best;
}

/**
 * Scale origin for the open/close animation: the float grows out of (and
 * shrinks back into) the corner it is anchored to.
 * @param anchor Snap position.
 * @returns A CSS `transform-origin` value.
 */
export function resolveFloatingTransformOrigin(anchor: FloatingAnchor): string {
  const x = anchor.includes('left') ? 'left' : anchor.includes('right') ? 'right' : 'center';
  const y = anchor.startsWith('top') ? 'top' : anchor.startsWith('bottom') ? 'bottom' : 'center';
  return `${x} ${y}`;
}

/**
 * Whether the float is offered at this viewport width.
 * @param viewportWidth Window width in px.
 * @returns True when the float may open.
 */
export function isFloatingAvailable(viewportWidth: number): boolean {
  return Number.isFinite(viewportWidth) && viewportWidth >= FLOATING_MIN_VIEWPORT_WIDTH_PX;
}

/**
 * Parse a persisted anchor, rejecting anything outside the known set.
 * @param raw A value read from storage.
 * @returns The anchor, or `null` for an unknown value.
 */
export function parseFloatingAnchor(raw: unknown): FloatingAnchor | null {
  return typeof raw === 'string' && (FLOATING_ANCHORS as readonly string[]).includes(raw)
    ? (raw as FloatingAnchor)
    : null;
}

/**
 * Share the middle column's freed width between the side columns that are
 * actually expanded: both expanded → equal halves (the odd pixel goes right);
 * one expanded → that one takes everything; none → nothing is widened.
 * @param leftBase The left column's stored width (0 when collapsed).
 * @param rightBase The right column's stored width (0 when collapsed).
 * @param shellWidth The flex row's width.
 * @returns Extra width to add to each side's rendered width.
 */
export function distributeSideExtra(leftBase: number, rightBase: number, shellWidth: number): SideExtraWidth {
  const left = Math.max(0, leftBase);
  const right = Math.max(0, rightBase);
  const extra = Math.max(0, shellWidth - left - right);
  if (left <= 0 && right <= 0) return { left: 0, right: 0 };
  // The right column absorbs the freed width whenever it is expanded; the left
  // column keeps its stored width. Only a left column with no right column
  // beside it grows into the space.
  if (right > 0) return { left: 0, right: extra };
  return { left: extra, right: 0 };
}
