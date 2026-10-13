import { describe, it, expect } from 'vitest';
import { anchorPanelLeft, TOOLBAR_POPOVER_EDGE_GAP } from '../toolbar-popover';

const base = { panelWidth: 300, viewportWidth: 1280 };

describe('anchorPanelLeft', () => {
  it('grows right from a trigger on the left half', () => {
    // The terminal entry sits just right of the media menu.
    expect(anchorPanelLeft({ ...base, triggerLeft: 106, triggerRight: 128 })).toBe(106);
  });

  it('grows left from a trigger on the right half', () => {
    // The context ring sits near the right edge.
    expect(anchorPanelLeft({ ...base, triggerLeft: 1180, triggerRight: 1202 })).toBe(902);
  });

  it('clamps into the viewport when the anchor would leave it', () => {
    // Right-half trigger, panel wider than the space behind it: it still fits,
    // because the anchor (the trigger's right edge) is inside the viewport.
    expect(anchorPanelLeft({ panelWidth: 600, viewportWidth: 800, triggerLeft: 600, triggerRight: 620 })).toBe(
      620 - 600
    );
    // A panel wider than the viewport sticks to the left gap.
    expect(anchorPanelLeft({ panelWidth: 900, viewportWidth: 800, triggerLeft: 10, triggerRight: 30 })).toBe(
      TOOLBAR_POPOVER_EDGE_GAP
    );
  });
});
