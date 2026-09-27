import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import {
  RIGHT_SIDEBAR_DEFAULT_WIDTH,
  RIGHT_SIDEBAR_MAX_WIDTH,
  RIGHT_SIDEBAR_MIN_WIDTH,
  clampSidebarWidth,
  useRightSidebarStore
} from '../right-sidebar';

describe('stores/right-sidebar', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('starts collapsed with no tabs', () => {
    const store = useRightSidebarStore();
    expect(store.collapsed).toBe(true);
    expect(store.tabs).toEqual([]);
    expect(store.activeTabId).toBeNull();
  });

  it('openTab() adds, activates and expands', () => {
    const store = useRightSidebarStore();

    const id = store.openTab('logs');

    expect(store.tabs.map(t => t.kind)).toEqual(['logs']);
    expect(store.activeTabId).toBe(id);
    expect(store.collapsed).toBe(false);
  });

  it('openTab() allows the same panel twice with distinct ids', () => {
    const store = useRightSidebarStore();

    const first = store.openTab('logs');
    const second = store.openTab('logs');

    expect(first).not.toBe(second);
    expect(store.tabs).toHaveLength(2);
    expect(store.activeTabId).toBe(second);
  });

  it('activateTab() switches to an open tab only', () => {
    const store = useRightSidebarStore();
    const logs = store.openTab('logs');
    store.openTab('stats');

    store.activateTab(logs);
    expect(store.activeTabId).toBe(logs);

    store.activateTab('missing');
    expect(store.activeTabId).toBe(logs);
  });

  it('closeTab() activates the right neighbour, else the left one', () => {
    const store = useRightSidebarStore();
    const first = store.openTab('logs');
    const second = store.openTab('stats');
    const third = store.openTab('logs');

    store.activateTab(second);
    store.closeTab(second);
    expect(store.activeTabId).toBe(third);

    store.closeTab(third);
    expect(store.activeTabId).toBe(first);

    store.closeTab(first);
    expect(store.activeTabId).toBeNull();
    expect(store.tabs).toEqual([]);
  });

  it('closeTab() keeps the active tab when another one closes', () => {
    const store = useRightSidebarStore();
    const first = store.openTab('logs');
    const second = store.openTab('stats');

    store.closeTab(first);

    expect(store.activeTabId).toBe(second);
  });

  it('clampSidebarWidth() keeps the panel inside its band', () => {
    // Wide viewport: only the fixed band applies.
    expect(clampSidebarWidth(50, 2000)).toBe(RIGHT_SIDEBAR_MIN_WIDTH);
    expect(clampSidebarWidth(5000, 2000)).toBe(RIGHT_SIDEBAR_MAX_WIDTH);
    expect(clampSidebarWidth(500, 2000)).toBe(500);
    // Narrow viewport: the 55% viewport cap wins over the fixed maximum.
    expect(clampSidebarWidth(700, 800)).toBe(440);
    // A viewport so narrow that 55% is below the minimum still yields something usable.
    expect(clampSidebarWidth(400, 400)).toBe(RIGHT_SIDEBAR_MIN_WIDTH);
  });

  it('setWidth() clamps every drag step and defaults to a comfortable width', () => {
    const store = useRightSidebarStore();
    expect(store.width).toBe(RIGHT_SIDEBAR_DEFAULT_WIDTH);

    store.setWidth(100, 2000);
    expect(store.width).toBe(RIGHT_SIDEBAR_MIN_WIDTH);

    store.setWidth(9999, 2000);
    expect(store.width).toBe(RIGHT_SIDEBAR_MAX_WIDTH);

    store.setWidth(512, 2000);
    expect(store.width).toBe(512);
  });

  it('fitToViewport() pulls a saved width back inside a shrunken window', () => {
    const store = useRightSidebarStore();

    // 700 fits a 2000px window, but not an 800px one (cap = 440).
    store.setWidth(700, 2000);
    expect(store.width).toBe(700);

    store.fitToViewport(800);
    expect(store.width).toBe(440);

    // No-op while the width still fits.
    store.setWidth(400, 2000);
    store.fitToViewport(2000);
    expect(store.width).toBe(400);
  });

  it('toggle() flips the collapse flag; expand() only opens', () => {
    const store = useRightSidebarStore();

    store.toggle();
    expect(store.collapsed).toBe(false);
    store.toggle();
    expect(store.collapsed).toBe(true);

    store.expand();
    expect(store.collapsed).toBe(false);
    store.expand();
    expect(store.collapsed).toBe(false);
  });
});
