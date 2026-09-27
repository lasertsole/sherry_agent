import { defineStore } from 'pinia';

/** Panel kinds the right sidebar can host as tabs. */
export type RightSidebarPanelKind = 'logs' | 'stats' | 'knowledgeGraph';

/** Narrowest usable sidebar (below this the panels get unusable). */
export const RIGHT_SIDEBAR_MIN_WIDTH = 280;
/** Widest allowed sidebar: a drag can never swallow more than this (or ~half of a narrow window). */
export const RIGHT_SIDEBAR_MAX_WIDTH = 760;
/** Default width for a fresh profile. */
export const RIGHT_SIDEBAR_DEFAULT_WIDTH = 420;
/** Laying-out floor of a panel body: narrower than this it scrolls sideways instead of squeezing. */
export const RIGHT_SIDEBAR_PANEL_MIN_WIDTH = 420;
/** Laying-out floor of a panel body: shorter than this it scrolls vertically instead of clipping. */
export const RIGHT_SIDEBAR_PANEL_MIN_HEIGHT = 360;

/**
 * Clamp a requested width into the allowed band, also keeping the panel below
 * ~55% of the viewport so it can never crowd the chat out on small screens.
 * @param width Requested width in px.
 * @param viewportWidth Current window width (defaults to `window.innerWidth`).
 */
export function clampSidebarWidth(
  width: number,
  viewportWidth: number = typeof window === 'undefined' ? 0 : window.innerWidth
): number {
  const viewportCap = viewportWidth > 0 ? Math.floor(viewportWidth * 0.55) : RIGHT_SIDEBAR_MAX_WIDTH;
  const cap = Math.max(RIGHT_SIDEBAR_MIN_WIDTH, Math.min(RIGHT_SIDEBAR_MAX_WIDTH, viewportCap));
  return Math.round(Math.min(cap, Math.max(RIGHT_SIDEBAR_MIN_WIDTH, width)));
}

/** One open tab of the right sidebar. */
export interface RightSidebarTab {
  /** Unique per tab instance: the same panel can be added more than once. */
  id: string;
  /** Which panel component the tab renders. */
  kind: RightSidebarPanelKind;
}

let tabSeq = 0;

/**
 * Right-sidebar state: collapsed flag (persisted, mirroring the session
 * sidebar) plus the open tab list and the active tab (in-memory — a refresh
 * starts with an empty strip).
 *
 * The sidebar lives in the home shell, so its tabs survive session switches;
 * panels are lazily imported by the component and mounted only while active.
 */
export const useRightSidebarStore = defineStore(
  'rightSidebar',
  () => {
    /** Whether the sidebar is retracted (default: retracted, no wasted width). */
    const collapsed = ref(true);
    /** Open tabs in strip order. */
    const tabs = ref<RightSidebarTab[]>([]);
    /** Active tab id, or null when nothing is open. */
    const activeTabId = ref<string | null>(null);
    /** Panel width in px (draggable, clamped; persisted). */
    const width = ref(RIGHT_SIDEBAR_DEFAULT_WIDTH);

    /** Collapse/expand the sidebar (top-bar toggle). */
    function toggle(): void {
      collapsed.value = !collapsed.value;
    }

    /** Expand without toggling (used when a menu entry opens a tab). */
    function expand(): void {
      collapsed.value = false;
    }

    /**
     * Set the panel width from a drag (clamped into the allowed band).
     * @param next Requested width in px.
     * @param viewportWidth Current window width (tests pass it explicitly).
     */
    function setWidth(next: number, viewportWidth?: number): void {
      width.value = clampSidebarWidth(next, viewportWidth);
    }

    /**
     * Re-clamp the stored width against the viewport, e.g. after the window
     * shrank past the cap the width was saved under (no-op while it still fits).
     * @param viewportWidth Current window width.
     */
    function fitToViewport(viewportWidth: number): void {
      const next = clampSidebarWidth(width.value, viewportWidth);
      if (next !== width.value) width.value = next;
    }

    /**
     * Add a tab of the given kind, activate it and expand the sidebar.
     * Always a NEW tab (and a fresh panel instance) so the same panel can be
     * opened twice — e.g. one log view per source.
     * @param kind Panel kind to add.
     * @returns The new tab id.
     */
    function openTab(kind: RightSidebarPanelKind): string {
      const id = `${kind}-${++tabSeq}`;
      tabs.value = [...tabs.value, { id, kind }];
      activeTabId.value = id;
      expand();
      return id;
    }

    /**
     * Make an open tab active (no-op for unknown ids).
     * @param id Tab id.
     */
    function activateTab(id: string): void {
      if (tabs.value.some(tab => tab.id === id)) activeTabId.value = id;
    }

    /**
     * Close a tab; the neighbour to its right (else the left) becomes active,
     * and the last tab closing leaves no active tab (the empty state shows).
     * @param id Tab id.
     */
    function closeTab(id: string): void {
      const index = tabs.value.findIndex(tab => tab.id === id);
      if (index === -1) return;
      const remaining = [...tabs.value];
      remaining.splice(index, 1);
      tabs.value = remaining;
      if (activeTabId.value === id) {
        const neighbour = remaining[index] ?? remaining[index - 1] ?? null;
        activeTabId.value = neighbour?.id ?? null;
      }
    }

    return {
      collapsed,
      tabs,
      activeTabId,
      width,
      toggle,
      expand,
      setWidth,
      fitToViewport,
      openTab,
      activateTab,
      closeTab
    };
  },
  {
    persist: {
      pick: ['collapsed', 'width']
    }
  }
);
