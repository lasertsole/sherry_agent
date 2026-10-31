import { describe, it, expect } from 'vitest';
import {
  DEFAULT_FLOATING_ANCHOR,
  FLOATING_ANCHORS,
  FLOATING_DRAG_CLICK_THRESHOLD_PX,
  FLOATING_MARGIN_PX,
  FLOATING_RESIZE_HEIGHT_MIN_PX,
  FLOATING_RESIZE_WIDTH_MAX_PX,
  FLOATING_RESIZE_WIDTH_MIN_PX,
  FLOATING_WIDTH_MAX_PX,
  clampCollapsedHeight,
  clampFloatingPosition,
  distributeSideExtra,
  isFloatingAvailable,
  parseFloatingAnchor,
  resolveFloatingPosition,
  resolveFloatingSize,
  resolveFloatingTransformOrigin,
  resolveNearestFloatingAnchor
} from '../floating-window';

/** The shell at 1280×720 (the spike's viewport). */
const SLOT = { left: 0, top: 0, width: 1280, height: 720 };

describe('resolveFloatingSize', () => {
  it('takes 45% × 78% of the slot at 1280×720', () => {
    expect(resolveFloatingSize(SLOT)).toEqual({ width: 576, height: 562 });
  });

  it('caps a large display at the max size', () => {
    expect(resolveFloatingSize({ left: 0, top: 0, width: 1920, height: 1440 })).toEqual({
      width: FLOATING_WIDTH_MAX_PX,
      height: 820
    });
  });

  it('keeps the minimum unless the slot itself is narrower', () => {
    // 400 - 32 = 368 wins over the 480 floor: a float wider than its slot is useless.
    expect(resolveFloatingSize({ left: 0, top: 0, width: 400, height: 300 })).toEqual({
      width: 368,
      height: 268
    });
  });

  it('never returns a negative box for a zero slot', () => {
    expect(resolveFloatingSize({ left: 0, top: 0, width: 0, height: 0 })).toEqual({ width: 0, height: 0 });
  });

  it('adopts a user-resized size inside the wider drag band', () => {
    expect(resolveFloatingSize({ left: 0, top: 0, width: 1440, height: 1000 }, { width: 900, height: 700 })).toEqual({
      width: 900,
      height: 700
    });
  });

  it('clamps a user-resized size to the drag bounds and the slot', () => {
    expect(resolveFloatingSize(SLOT, { width: 100, height: 100 })).toEqual({
      width: FLOATING_RESIZE_WIDTH_MIN_PX,
      height: FLOATING_RESIZE_HEIGHT_MIN_PX
    });
    expect(resolveFloatingSize(SLOT, { width: 5000, height: 5000 })).toEqual({
      width: FLOATING_RESIZE_WIDTH_MAX_PX,
      height: SLOT.height - FLOATING_MARGIN_PX * 2
    });
  });
});

describe('clampCollapsedHeight', () => {
  it('uses the measured composer height as the input-only bar', () => {
    expect(clampCollapsedHeight(160, SLOT)).toBe(160);
  });

  it('caps the bar at the slot and never goes negative', () => {
    expect(clampCollapsedHeight(2000, SLOT)).toBe(688);
    expect(clampCollapsedHeight(-10, SLOT)).toBe(0);
  });
});

describe('resolveFloatingPosition', () => {
  const size = { width: 576, height: 562 };
  const at = (anchor: Parameters<typeof resolveFloatingPosition>[0]) => resolveFloatingPosition(anchor, SLOT, size);

  it('centers the default anchor', () => {
    expect(DEFAULT_FLOATING_ANCHOR).toBe('center');
    expect(at('center')).toEqual({ left: 352, top: 79 });
  });

  it('keeps a 16px margin on every edge', () => {
    expect(at('top-left')).toEqual({ left: 16, top: 16 });
    expect(at('top-center')).toEqual({ left: 352, top: 16 });
    expect(at('top-right')).toEqual({ left: 1280 - 16 - 576, top: 16 });
    expect(at('left-center')).toEqual({ left: 16, top: 79 });
    expect(at('right-center')).toEqual({ left: 688, top: 79 });
    expect(at('bottom-left')).toEqual({ left: 16, top: 720 - 16 - 562 });
    expect(at('bottom-center')).toEqual({ left: 352, top: 142 });
    expect(at('bottom-right')).toEqual({ left: 688, top: 142 });
  });

  it('has nine anchors and no duplicates', () => {
    expect(FLOATING_ANCHORS).toHaveLength(9);
    expect(new Set(FLOATING_ANCHORS).size).toBe(9);
  });
});

describe('clampFloatingPosition', () => {
  it('keeps a dragged box inside the slot', () => {
    const size = { width: 576, height: 562 };
    expect(clampFloatingPosition({ left: -100, top: -100 }, SLOT, size)).toEqual({ left: 0, top: 0 });
    expect(clampFloatingPosition({ left: 9999, top: 9999 }, SLOT, size)).toEqual({
      left: 1280 - 576,
      top: 720 - 562
    });
  });

  it('does not go negative when the box is larger than the slot', () => {
    expect(
      clampFloatingPosition(
        { left: 50, top: 50 },
        { left: 0, top: 0, width: 100, height: 100 },
        { width: 200, height: 200 }
      )
    ).toEqual({
      left: 0,
      top: 0
    });
  });
});

describe('resolveNearestFloatingAnchor', () => {
  const size = { width: 576, height: 562 };

  it('falls back to the default for an exact center drop', () => {
    expect(resolveNearestFloatingAnchor({ left: 352, top: 79 }, SLOT, size)).toBe('center');
  });

  it('snaps a top-left drop to the top-left anchor', () => {
    expect(resolveNearestFloatingAnchor({ left: 20, top: 20 }, SLOT, size)).toBe('top-left');
  });

  it('snaps a bottom-right drop to the bottom-right anchor', () => {
    expect(resolveNearestFloatingAnchor({ left: 690, top: 140 }, SLOT, size)).toBe('bottom-right');
  });

  it('snaps a left-middle drop to left-center', () => {
    expect(resolveNearestFloatingAnchor({ left: 18, top: 80 }, SLOT, size)).toBe('left-center');
  });
});

describe('resolveFloatingTransformOrigin', () => {
  it('maps each side of the anchor to a CSS origin', () => {
    expect(resolveFloatingTransformOrigin('top-left')).toBe('left top');
    expect(resolveFloatingTransformOrigin('top-center')).toBe('center top');
    expect(resolveFloatingTransformOrigin('right-center')).toBe('right center');
    expect(resolveFloatingTransformOrigin('center')).toBe('center center');
    expect(resolveFloatingTransformOrigin('bottom-right')).toBe('right bottom');
  });
});

describe('isFloatingAvailable', () => {
  it('needs a 640px viewport', () => {
    expect(isFloatingAvailable(640)).toBe(true);
    expect(isFloatingAvailable(639)).toBe(false);
    expect(isFloatingAvailable(Number.NaN)).toBe(false);
  });
});

describe('parseFloatingAnchor', () => {
  it('accepts known anchors and rejects everything else', () => {
    expect(parseFloatingAnchor('bottom-left')).toBe('bottom-left');
    expect(parseFloatingAnchor('middle')).toBeNull();
    expect(parseFloatingAnchor(42)).toBeNull();
  });
});

describe('distributeSideExtra', () => {
  it('keeps the left width and gives the freed width to the right', () => {
    expect(distributeSideExtra(280, 552, 1280)).toEqual({ left: 0, right: 448 });
  });

  it('still gives the whole freed width to the right on an odd split', () => {
    expect(distributeSideExtra(280, 551, 1280)).toEqual({ left: 0, right: 449 });
  });

  it('gives the whole freed width to the only expanded side', () => {
    expect(distributeSideExtra(280, 0, 1280)).toEqual({ left: 1000, right: 0 });
    expect(distributeSideExtra(0, 552, 1280)).toEqual({ left: 0, right: 728 });
  });

  it('widens nothing when neither side is expanded', () => {
    expect(distributeSideExtra(0, 0, 1280)).toEqual({ left: 0, right: 0 });
  });

  it('never produces a negative share when the columns overflow', () => {
    expect(distributeSideExtra(700, 700, 1280)).toEqual({ left: 0, right: 0 });
  });

  it('keeps the margin constant', () => {
    expect(FLOATING_MARGIN_PX).toBe(16);
    expect(FLOATING_DRAG_CLICK_THRESHOLD_PX).toBe(4);
  });
});
