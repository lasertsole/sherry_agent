import { describe, it, expect } from 'vitest';

import { TITLEBAR_HEIGHT, TITLEBAR_HEIGHT_CSS } from '../titlebar';

/**
 * The custom title bar's height is the single source for the home shell's top
 * padding, the right sidebar's maximized overlay and the floating window's
 * slot. It is also baked into the component as `h-9`; this pins the number the
 * rest of the layout reasons about.
 */
describe('titlebar geometry', () => {
  it('is 36px (Tailwind h-9)', () => {
    expect(TITLEBAR_HEIGHT).toBe(36);
    expect(TITLEBAR_HEIGHT_CSS).toBe('36px');
  });
});
