/**
 * The tab strip's overflow model (unit).
 *
 * The strip is equal-width and shrinks proportionally, so it only "overflows"
 * once every tab sits at its 60px floor. These are the two decisions the
 * component makes from that measurement: does it scroll at all, and which side
 * is cut off (the fade masks).
 */

import { describe, expect, it } from 'vitest';
import {
  TAB_GAP_PX,
  TAB_MIN_WIDTH_PX,
  minStripWidth,
  tabMaskSides,
  tabsOverflow
} from '@/composables/use-tabs-overflow';

describe('minStripWidth', () => {
  it('is zero for an empty strip', () => {
    expect(minStripWidth(0)).toBe(0);
  });

  it('is one floor tab without a gap', () => {
    expect(minStripWidth(1)).toBe(TAB_MIN_WIDTH_PX);
  });

  it('counts one gap between neighbours', () => {
    expect(minStripWidth(3)).toBe(3 * TAB_MIN_WIDTH_PX + 2 * TAB_GAP_PX);
    expect(TAB_MIN_WIDTH_PX).toBe(60);
    expect(TAB_GAP_PX).toBe(4);
  });
});

describe('tabsOverflow', () => {
  it('does not overflow while the floor fits', () => {
    // 3 tabs = 188px; 200px of viewport still holds them.
    expect(tabsOverflow(3, 200)).toBe(false);
  });

  it('overflows once the floor no longer fits', () => {
    expect(tabsOverflow(3, 120)).toBe(true);
  });

  it('tolerates a pixel of layout rounding', () => {
    // 188px floor in a 187px viewport: sub-pixel rounding, not a real overflow.
    expect(tabsOverflow(3, minStripWidth(3) - 1)).toBe(false);
    expect(tabsOverflow(3, minStripWidth(3) - 2)).toBe(true);
  });
});

describe('tabMaskSides', () => {
  it('shows no mask when the strip does not scroll', () => {
    expect(tabMaskSides(0, 320, 320, false)).toEqual({ left: false, right: false });
    // A non-scrolling viewport keeps both masks off even if the offsets look odd.
    expect(tabMaskSides(120, 320, 320, false)).toEqual({ left: false, right: false });
  });

  it('shows only the right mask at the start of a scrolled strip', () => {
    expect(tabMaskSides(0, 320, 200, true)).toEqual({ left: false, right: true });
  });

  it('shows both masks in the middle', () => {
    expect(tabMaskSides(60, 320, 200, true)).toEqual({ left: true, right: true });
  });

  it('shows only the left mask at the end', () => {
    expect(tabMaskSides(120, 320, 200, true)).toEqual({ left: true, right: false });
  });

  it('tolerates a pixel at either boundary', () => {
    expect(tabMaskSides(1, 320, 200, true)).toEqual({ left: false, right: true });
    expect(tabMaskSides(119, 320, 200, true)).toEqual({ left: true, right: false });
  });
});
