import { describe, it, expect, beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import {
  SIDEBAR_LG_WIDTH,
  SIDEBAR_MAX_WIDTH_RATIO,
  SIDEBAR_MD_WIDTH,
  SIDEBAR_MIN_WIDTH,
  clampSidebarWidth,
  defaultSidebarWidth,
  useSidebarWidthStore
} from '../../stores/sidebar-width';

/**
 * The left column's band: a 280px floor (the old md width — nothing that fit
 * before may now clip) and half the viewport at most, minus whatever the rest
 * of the shell needs. These are the numbers the drag can never leave.
 */
describe('clampSidebarWidth', () => {
  it('keeps a width inside the band untouched', () => {
    expect(clampSidebarWidth(400, 1600)).toBe(400);
  });

  it('never goes below the 280px floor', () => {
    expect(clampSidebarWidth(120, 1600)).toBe(SIDEBAR_MIN_WIDTH);
  });

  it('never exceeds half the viewport', () => {
    expect(SIDEBAR_MAX_WIDTH_RATIO).toBe(0.5);
    expect(clampSidebarWidth(1200, 1600)).toBe(800);
  });

  it('leaves room for the conversation and the right column', () => {
    // 1600px window, 800px is the ratio cap, but 320 (conversation) + 240
    // (right column) are reserved: the ceiling becomes 1040 → still 800 here.
    expect(clampSidebarWidth(1200, 1600, 560)).toBe(800);
    // 1000px window: ratio cap 500, room cap 1000-560 = 440 → the room wins.
    expect(clampSidebarWidth(1200, 1000, 560)).toBe(440);
  });

  it('lets the ceiling win when the window cannot hold the floor', () => {
    // A 500px window has no room for a 280px column plus the conversation: a
    // column wider than the window helps nobody, and auto-collapse is closing
    // the side columns there anyway.
    expect(clampSidebarWidth(400, 500, 400)).toBe(100);
  });

  it('rounds to whole pixels', () => {
    expect(clampSidebarWidth(400.6, 1600)).toBe(401);
  });
});

describe('defaultSidebarWidth', () => {
  it('opens at the old lg width on a desktop window', () => {
    expect(defaultSidebarWidth(1440)).toBe(SIDEBAR_LG_WIDTH);
  });

  it('opens at the old md width below the breakpoint', () => {
    expect(defaultSidebarWidth(900)).toBe(SIDEBAR_MD_WIDTH);
  });
});

describe('useSidebarWidthStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('clamps every write', () => {
    const store = useSidebarWidthStore();

    store.setWidth(50, 1600);
    expect(store.width).toBe(SIDEBAR_MIN_WIDTH);

    store.setWidth(900, 1600);
    expect(store.width).toBe(800);
  });

  it('re-clamps a stored width against a shrunken viewport', () => {
    const store = useSidebarWidthStore();
    store.setWidth(700, 1600);
    expect(store.width).toBe(700);

    store.fitToViewport(800);

    expect(store.width).toBe(400);
  });

  it('leaves a width that still fits alone', () => {
    const store = useSidebarWidthStore();
    store.setWidth(400, 1600);

    store.fitToViewport(1600);

    expect(store.width).toBe(400);
  });
});
