import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import {
  RIGHT_SIDEBAR_DEFAULT_WIDTH,
  RIGHT_SIDEBAR_MAX_WIDTH,
  RIGHT_SIDEBAR_MIN_WIDTH,
  RIGHT_SIDEBAR_WIDE_PANEL_WIDTH,
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

  it('toggleMaximized() flips the mode and expands a collapsed panel', () => {
    const store = useRightSidebarStore();
    store.collapsed = true;

    store.toggleMaximized();
    expect(store.maximized).toBe(true);
    expect(store.collapsed).toBe(false);

    store.toggleMaximized();
    expect(store.maximized).toBe(false);
    // Collapse is NOT restored: the toggle only ever expands, mirroring its
    // sibling controls (a user who collapsed first keeps the panel visible).
    expect(store.collapsed).toBe(false);
  });

  it('openTab() adds, activates and expands', () => {
    const store = useRightSidebarStore();

    const id = store.openTab('logs');

    expect(store.tabs.map(t => t.kind)).toEqual(['logs']);
    expect(store.activeTabId).toBe(id);
    expect(store.collapsed).toBe(false);
  });

  it('files every tab under a group scope and shows the tab s group on open', () => {
    const store = useRightSidebarStore();

    // Every settings/runtime tool is a GLOBAL tab; the things read from the
    // active session (its preset, a file of its project directory) are session
    // tabs, and the strip starts on 全局.
    expect(store.activeScope).toBe('global');
    const logs = store.openTab('logs');
    expect(store.tabs.find(tab => tab.id === logs)?.scope).toBe('global');
    expect(store.tabsInScope('global').map(tab => tab.kind)).toEqual(['logs']);
    expect(store.openTab('fileViewer', { path: 'src/main.py' })).not.toBe(logs);
    expect(store.tabs.find(tab => tab.kind === 'fileViewer')?.scope).toBe('session');
    expect(store.activeScope).toBe('session');
    store.setActiveScope('global');

    store.setActiveScope('session');
    expect(store.activeScope).toBe('session');
    expect(store.tabsInScope('session').map(tab => tab.kind)).toEqual(['fileViewer']);

    // Opening a tab jumps to its own group (and so does activating a closed one).
    const preset = store.openTab('sessionPreset');
    expect(store.activeScope).toBe('session');
    expect(store.tabsInScope('session').map(tab => tab.kind)).toEqual(['fileViewer', 'sessionPreset']);

    // The toolbox panels are session-scoped as well (the terminal's cwd IS the
    // session's project directory) and dedupe by kind.
    const browser = store.openTab('browser', { instance: 'browser#1' });
    expect(store.tabs.find(tab => tab.id === browser)?.scope).toBe('session');
    // Same instance → the same tab; a NEW instance key → a twin (the toolbox
    // opens one browser / terminal per click).
    expect(store.openTab('browser', { instance: 'browser#1' })).toBe(browser);
    const secondBrowser = store.openTab('browser', { instance: 'browser#2' });
    expect(secondBrowser).not.toBe(browser);
    expect(store.tabs.filter(tab => tab.kind === 'browser')).toHaveLength(2);
    const terminal = store.openTab('terminal', { instance: 'terminal#1' });
    expect(store.tabs.find(tab => tab.id === terminal)?.scope).toBe('session');
    expect(store.openTab('terminal', { instance: 'terminal#1' })).toBe(terminal);

    // The context viewer is session-scoped too, and every click (any figure in
    // any bubble) lands in ONE instance: no payload, so the kind dedupes.
    const viewer = store.openTab('contextViewer');
    expect(store.tabs.find(tab => tab.id === viewer)?.scope).toBe('session');
    expect(store.openTab('contextViewer')).toBe(viewer);

    store.setActiveScope('global');
    store.activateTab(preset);
    expect(store.activeScope).toBe('session');

    // Closing the session tab falls back to its left neighbour — which stays in
    // the SESSION group, whatever kind it is (the group has grown over time:
    // file viewer, context viewer, browser, terminal).
    store.closeTab(preset);
    expect(store.tabs.find(tab => tab.id === store.activeTabId)?.scope).toBe('session');
    expect(store.activeScope).toBe('session');

    // Closing the session tabs one by one walks the whole group before landing
    // back on the global one.
    while (store.tabs.find(tab => tab.id === store.activeTabId)?.scope === 'session') {
      store.closeTab(store.activeTabId!);
    }
    expect(store.activeTabId).toBe(logs);
    expect(store.activeScope).toBe('global');
  });

  it('openTab() reuses an existing tab of the same kind instead of duplicating it', () => {
    // The menu must never stack a second 系统配置 next to the open one — the
    // click belongs to the tab that already exists.
    const store = useRightSidebarStore();

    const logs = store.openTab('logs');
    store.openTab('stats');
    const again = store.openTab('logs');

    expect(again).toBe(logs);
    expect(store.tabs.map(t => t.kind)).toEqual(['logs', 'stats']);
    expect(store.activeTabId).toBe(logs);
  });

  it('openTab() re-expands and re-widens when it reuses a tab', () => {
    const store = useRightSidebarStore();
    // The widened width is still clamped to the viewport (happy-dom's window).
    const wide = clampSidebarWidth(RIGHT_SIDEBAR_WIDE_PANEL_WIDTH);
    const first = store.openTab('systemConfig');
    expect(store.width).toBe(wide);

    store.toggle();
    expect(store.collapsed).toBe(true);
    store.setWidth(RIGHT_SIDEBAR_MIN_WIDTH, 2000);

    expect(store.openTab('systemConfig')).toBe(first);
    expect(store.tabs).toHaveLength(1);
    expect(store.collapsed).toBe(false);
    expect(store.width).toBe(wide);
  });

  it('openTab() carries a per-instance payload (two file tabs, two paths)', () => {
    const store = useRightSidebarStore();

    const first = store.openTab('fileViewer', { path: 'src/main.py' });
    const second = store.openTab('fileViewer', { path: 'README.md' });

    const tabs = store.tabs.filter(t => t.kind === 'fileViewer');
    expect(tabs.map(t => t.payload?.path)).toEqual(['src/main.py', 'README.md']);
    expect(first).not.toBe(second);
    expect(store.tabs.find(t => t.id === second)?.payload?.path).toBe('README.md');
  });

  it('openTab() reuses the same file tab (same path is the same tab)', () => {
    const store = useRightSidebarStore();

    const first = store.openTab('fileViewer', { path: 'src/main.py' });
    store.openTab('fileViewer', { path: 'README.md' });

    expect(store.openTab('fileViewer', { path: 'src/main.py' })).toBe(first);
    expect(store.tabs).toHaveLength(2);
    expect(store.activeTabId).toBe(first);
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
    const third = store.openTab('memory');

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
