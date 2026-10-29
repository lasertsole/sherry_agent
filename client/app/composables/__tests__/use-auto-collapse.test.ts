import { describe, it, expect } from 'vitest';

import {
  COLLAPSE_SIDEBAR_PX,
  COLLAPSE_SIDE_PANE_PX,
  autoCollapseTarget,
  conversationWidth,
  shouldCollapseLeft
} from '../use-auto-collapse';

/**
 * The narrow-window safety net: the conversation must stay readable, so the
 * accessory columns retract — right first (the accessory), left second (the
 * navigation). Both thresholds are ZCode's.
 */
describe('conversationWidth', () => {
  it('subtracts the expanded columns only', () => {
    expect(conversationWidth(1440, { collapsed: false, width: 360 }, { collapsed: false, width: 420 })).toBe(660);
  });

  it('ignores a collapsed column', () => {
    expect(conversationWidth(1440, { collapsed: true, width: 360 }, { collapsed: false, width: 420 })).toBe(1020);
  });
});

describe('autoCollapseTarget', () => {
  const left = { collapsed: false, width: 280 };
  const right = { collapsed: false, width: 420 };

  it('does nothing on a wide window', () => {
    expect(autoCollapseTarget(1440, left, right)).toBeNull();
  });

  it('retracts the right column first', () => {
    // 1000 - 280 - 420 = 300 < 480
    expect(autoCollapseTarget(1000, left, right)).toBe('right');
  });

  it('leaves an already-collapsed right column alone', () => {
    expect(autoCollapseTarget(1000, left, { collapsed: true, width: 420 })).toBeNull();
  });

  it('never targets the left column from this decision', () => {
    // Both thresholds are tripped, but the right column goes first and the
    // left one is re-measured after it is gone.
    expect(autoCollapseTarget(700, left, right)).toBe('right');
  });
});

describe('shouldCollapseLeft', () => {
  it('is false on a wide window', () => {
    expect(shouldCollapseLeft(1440, { collapsed: false, width: 360 })).toBe(false);
  });

  it('retracts below the 360px conversation floor', () => {
    expect(COLLAPSE_SIDEBAR_PX).toBe(360);
    expect(shouldCollapseLeft(600, { collapsed: false, width: 360 })).toBe(true);
  });

  it('is a no-op when already collapsed', () => {
    expect(shouldCollapseLeft(600, { collapsed: true, width: 360 })).toBe(false);
  });
});

describe('the thresholds', () => {
  it('match the ZCode shell constants', () => {
    expect(COLLAPSE_SIDE_PANE_PX).toBe(480);
    expect(COLLAPSE_SIDEBAR_PX).toBe(360);
  });
});
