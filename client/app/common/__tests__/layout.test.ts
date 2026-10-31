import { describe, it, expect } from 'vitest';

import { MIDDLE_MIN_WIDTH_PX, MIDDLE_MIN_WIDTH_RATIO, middleMinWidth, middleMinWidthCss } from '../layout';

/**
 * The conversation's floor: an absolute 320px (a message row needs it) and a
 * proportional 35% (on a large display the chat keeps its share). The right
 * column's drag ceiling reads the same numbers, so they must agree with the CSS
 * the middle column is rendered with.
 */
describe('middleMinWidth', () => {
  it('keeps the absolute floor on a narrow window', () => {
    expect(middleMinWidth(800)).toBe(MIDDLE_MIN_WIDTH_PX);
  });

  it('follows the ratio once 35% exceeds the floor', () => {
    expect(middleMinWidth(1280)).toBe(448);
    expect(middleMinWidth(2560)).toBe(896);
  });

  it('falls back to the absolute floor without a usable viewport', () => {
    expect(middleMinWidth(0)).toBe(MIDDLE_MIN_WIDTH_PX);
    expect(middleMinWidth(Number.NaN)).toBe(MIDDLE_MIN_WIDTH_PX);
  });

  it('renders as a CSS max() the browser resolves itself', () => {
    expect(middleMinWidthCss()).toBe(`max(${MIDDLE_MIN_WIDTH_PX}px, ${MIDDLE_MIN_WIDTH_RATIO * 100}%)`);
  });
});
