import { defineStore } from 'pinia';

/**
 * Panel kinds the right sidebar can host as tabs: the three viewers plus every
 * settings-menu entry, so a tool stays open next to the chat.
 */
export type RightSidebarPanelKind =
  | 'logs'
  | 'stats'
  | 'knowledgeGraph'
  | 'skills'
  | 'systemConfig'
  | 'persona'
  | 'memory'
  | 'heartbeat'
  | 'cron'
  | 'extend'
  | 'taskDetail'
  | 'fileViewer'
  | 'account'
  | 'notification'
  /** The session's own persona preset (read-only). */
  | 'sessionPreset'
  /** One file's diff from one commit of the session project (opened from the git graph). */
  | 'gitDiff'
  /** The session's live context: system prompt, tools and message list. */
  | 'contextViewer'
  /** The toolbox's browser: a URL bar over an embedded page. */
  | 'browser'
  /** The toolbox's user terminal: commands typed by the operator. */
  | 'terminal';

/**
 * Which group a tab belongs to: the CURRENT SESSION's own views (things read from
 * the active session — its persona preset, a file of its project directory, a
 * commit diff of that directory) versus the process-wide tools (settings editors
 * and the runtime viewers), which apply everywhere.
 */
export type RightSidebarScope = 'session' | 'global';

/**
 * Scope per panel kind — the strip groups tabs by this, and callers never pass a
 * scope (a kind has exactly one home). What a session's own directory produced is
 * session-scoped: the preset viewer, the file viewer (a file of that directory)
 * and a commit diff read from it.
 */
const SCOPE_BY_KIND: Record<RightSidebarPanelKind, RightSidebarScope> = {
  logs: 'global',
  stats: 'global',
  knowledgeGraph: 'global',
  skills: 'global',
  systemConfig: 'global',
  persona: 'global',
  memory: 'global',
  heartbeat: 'global',
  cron: 'global',
  extend: 'global',
  taskDetail: 'global',
  // A file opened from the session's file tree belongs to that session, exactly
  // like the diff read from the same directory (gitDiff below).
  fileViewer: 'session',
  account: 'global',
  notification: 'global',
  sessionPreset: 'session',
  // A diff belongs to the session whose project directory it was read from.
  gitDiff: 'session',
  // The live context is read from the session's own checkpoint.
  contextViewer: 'session',
  // The browser and the terminal work on the session's own project (the
  // terminal's cwd IS that directory), so both live in its group.
  browser: 'session',
  terminal: 'session'
};

/**
 * Width an editor panel is opened with when the sidebar is narrower: those
 * bodies were built as 800-1600px modals, so the default 420 leaves them
 * single-column and cramped. Still inside the allowed band and still draggable.
 */
export const RIGHT_SIDEBAR_WIDE_PANEL_WIDTH = 640;

/** Kinds whose body is a full editor rather than a viewer. */
const WIDE_PANEL_KINDS: ReadonlySet<RightSidebarPanelKind> = new Set([
  'account',
  'skills',
  'systemConfig',
  'persona',
  'memory',
  'heartbeat',
  'cron',
  'extend',
  'taskDetail'
]);

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
  /**
   * Unique per tab. A kind appears at most once per payload: the strip never
   * shows two tabs of the same panel unless the payloads differ (two files).
   */
  id: string;
  /** Which panel component the tab renders. */
  kind: RightSidebarPanelKind;
  /** Which group tab the tab sits under (当前会话 / 全局). */
  scope: RightSidebarScope;
  /**
   * Per-instance data for kinds that need it (the file viewer's relative path).
   * The tab id is already the instance key, so the payload rides the tab itself
   * rather than a parallel path-by-tab map.
   */
  payload?: { path: string; hash?: string };
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
    /**
     * Which group tab is shown. 全局 by default: every pre-existing panel is a
     * global tool, so an untouched sidebar keeps exactly its old behaviour.
     */
    const activeScope = ref<RightSidebarScope>('global');
    /** Panel width in px (draggable, clamped; persisted). */
    const width = ref(RIGHT_SIDEBAR_DEFAULT_WIDTH);
    /**
     * Whether the panel fills the whole viewport (the strip's 放大 button).
     * Persisted like ``collapsed``: the choice is a workspace mode, not a
     * transient hover, and a reload should not silently undo it.
     */
    const maximized = ref(false);

    /** Collapse/expand the sidebar (top-bar toggle). */
    function toggle(): void {
      collapsed.value = !collapsed.value;
    }

    /** Expand without toggling (used when a menu entry opens a tab). */
    function expand(): void {
      collapsed.value = false;
    }

    /** Maximize / restore the panel (fills the viewport while maximized). */
    function toggleMaximized(): void {
      maximized.value = !maximized.value;
      if (maximized.value) expand();
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
     * Open a tab of the given kind, activate it and expand the sidebar.
     *
     * DEDUPLICATED: a tab that already exists for the same kind AND the same
     * payload is ACTIVATED (and re-widened for editor kinds) instead of added a
     * second time — clicking the menu entry of an already-open panel must show
     * that panel, not stack a twin. Two tabs of one kind can only coexist with
     * DISTINCT payloads (two files in the viewer), which are not duplicates.
     * @param kind Panel kind to open.
     * @param payload The panel's identity: its `path` (and, for a commit diff, the
     *   `hash` it was opened from).
     * @param payload.path
     * @param payload.hash
     * @returns The tab id (the existing tab's id when it was reused).
     */
    function openTab(kind: RightSidebarPanelKind, payload?: { path: string; hash?: string }): string {
      const path = payload?.path ?? null;
      const hash = payload?.hash ?? null;
      const scope = SCOPE_BY_KIND[kind];
      const existing = tabs.value.find(
        tab => tab.kind === kind && (tab.payload?.path ?? null) === path && (tab.payload?.hash ?? null) === hash
      );
      if (existing) {
        activeTabId.value = existing.id;
        activeScope.value = existing.scope;
        expand();
        if (WIDE_PANEL_KINDS.has(kind)) setWidth(Math.max(width.value, RIGHT_SIDEBAR_WIDE_PANEL_WIDTH));
        return existing.id;
      }
      const id = `${kind}-${++tabSeq}`;
      tabs.value = [...tabs.value, payload ? { id, kind, scope, payload } : { id, kind, scope }];
      activeTabId.value = id;
      activeScope.value = scope;
      expand();
      if (WIDE_PANEL_KINDS.has(kind)) setWidth(Math.max(width.value, RIGHT_SIDEBAR_WIDE_PANEL_WIDTH));
      return id;
    }

    /**
     * Tabs of one group (the strip renders the active group's list).
     * @param scope Group to list.
     * @returns The tabs of that scope, in strip order.
     */
    function tabsInScope(scope: RightSidebarScope): RightSidebarTab[] {
      return tabs.value.filter(tab => tab.scope === scope);
    }

    /**
     * Show a group tab. When the active tab lives in the OTHER group the group's
     * first tab is activated, so the body always belongs to the group on screen
     * (an empty group shows its empty state instead).
     * @param scope Group to show.
     */
    function setActiveScope(scope: RightSidebarScope): void {
      activeScope.value = scope;
      const active = tabs.value.find(tab => tab.id === activeTabId.value);
      if (active && active.scope === scope) return;
      const first = tabs.value.find(tab => tab.scope === scope);
      if (first) activeTabId.value = first.id;
    }

    /**
     * Make an open tab active (no-op for unknown ids).
     * @param id Tab id.
     */
    function activateTab(id: string): void {
      const tab = tabs.value.find(candidate => candidate.id === id);
      if (!tab) return;
      activeTabId.value = id;
      activeScope.value = tab.scope;
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
        if (neighbour) activeScope.value = neighbour.scope;
      }
    }

    return {
      collapsed,
      tabs,
      activeTabId,
      activeScope,
      width,
      maximized,
      toggle,
      toggleMaximized,
      expand,
      setWidth,
      fitToViewport,
      openTab,
      tabsInScope,
      setActiveScope,
      activateTab,
      closeTab
    };
  },
  {
    persist: {
      pick: ['collapsed', 'width', 'maximized']
    }
  }
);
