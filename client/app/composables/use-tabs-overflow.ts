import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue';

/**
 * Overflow detection for the right sidebar's tab strip.
 *
 * The tabs are equal-width and shrink proportionally (basis 156px, floor
 * 60px), so "does the strip overflow?" is not "the tabs do not fit" — the
 * browser shrinks them first. It overflows only when N tabs at their FLOOR no
 * longer fit the viewport, at which point `overflow-x-auto` takes over and the
 * fade masks mark the cut-off side.
 *
 * A ResizeObserver on the viewport covers the sidebar drag and every window
 * resize; the tab count is watched separately because adding/removing a tab
 * changes the floor without resizing the viewport.
 */

/** The tab's flex floor (`min-w-15`, 15 × 0.25rem). */
export const TAB_MIN_WIDTH_PX = 60;
/** The strip's gap (`gap-1`, 1 × 0.25rem). */
export const TAB_GAP_PX = 4;
/** Sub-pixel slack: layout rounding must not produce a phantom overflow. */
const OVERFLOW_TOLERANCE_PX = 1;

/**
 * What the strip needs when every tab sits at its floor.
 * @param tabCount
 */
export function minStripWidth(tabCount: number): number {
  if (tabCount <= 0) return 0;
  return tabCount * TAB_MIN_WIDTH_PX + (tabCount - 1) * TAB_GAP_PX;
}

/**
 * Whether the strip at its floor is wider than the viewport.
 * @param tabCount Number of visible tabs.
 * @param viewportWidth Viewport content width in px.
 */
export function tabsOverflow(tabCount: number, viewportWidth: number): boolean {
  return minStripWidth(tabCount) > viewportWidth + OVERFLOW_TOLERANCE_PX;
}

/**
 * Which fade masks the current scroll position calls for.
 * @param scrollLeft Current horizontal scroll offset.
 * @param scrollWidth Total scrollable width.
 * @param clientWidth Visible width.
 * @param overflowing Whether the strip scrolls at all.
 */
export function tabMaskSides(
  scrollLeft: number,
  scrollWidth: number,
  clientWidth: number,
  overflowing: boolean
): { left: boolean; right: boolean } {
  if (!overflowing) return { left: false, right: false };
  return {
    left: scrollLeft > OVERFLOW_TOLERANCE_PX,
    right: scrollLeft < scrollWidth - clientWidth - OVERFLOW_TOLERANCE_PX
  };
}

/**
 * Attach the overflow/mask state to one tab viewport.
 * @param viewportRef The scrolling element (the tab row).
 * @param tabCount Reactive number of visible tabs.
 * @returns Whether the strip overflows and which sides need a fade.
 */
export function useTabsOverflow(viewportRef: Ref<HTMLElement | null>, tabCount: Ref<number>) {
  const isOverflowing = ref(false);
  const showLeftMask = ref(false);
  const showRightMask = ref(false);

  /** Re-measure the viewport and update the mask state. */
  function recompute(): void {
    const viewport = viewportRef.value;
    if (!viewport) return;
    const overflowing = tabsOverflow(tabCount.value, viewport.clientWidth);
    const sides = tabMaskSides(viewport.scrollLeft, viewport.scrollWidth, viewport.clientWidth, overflowing);
    isOverflowing.value = overflowing;
    showLeftMask.value = sides.left;
    showRightMask.value = sides.right;
  }

  let frame: number | null = null;
  /** Coalesce bursts (resize + scroll + tab changes) into one measure per frame. */
  const schedule = (): void => {
    if (frame !== null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      recompute();
    });
  };

  let observer: ResizeObserver | null = null;

  onMounted(() => {
    const viewport = viewportRef.value;
    if (!viewport) return;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(schedule);
      observer.observe(viewport);
    }
    viewport.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    // One synchronous measure: the first paint must not wait for a frame.
    recompute();
  });

  onBeforeUnmount(() => {
    observer?.disconnect();
    observer = null;
    viewportRef.value?.removeEventListener('scroll', schedule);
    window.removeEventListener('resize', schedule);
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
  });

  watch(tabCount, schedule);

  return { isOverflowing, showLeftMask, showRightMask, recompute };
}
