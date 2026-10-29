import { defineStore } from 'pinia';
import { watch } from 'vue';

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
  /** The web-search backends (Tavily / SearXNG) and their provider settings. */
  | 'webSearch'
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
  /** The session's trajectory ledger: one event timeline per turn. */
  | 'trajectory'
  /** The toolbox's browser: a URL bar over an embedded page. */
  | 'browser'
  /** The toolbox's user terminal: commands typed by the operator. */
  | 'terminal'
  /** Interface language (国际化): the app-wide locale switch, first in the 菜单. */
  | 'i18n'
  /** A helper session opened from a quoted chat selection (an ordinary session). */
  | 'sideChat';

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
  // The search-backend panel is a settings editor like the rest: its provider
  // settings are global (.env), and the per-session choice reads the open
  // session when there is one.
  webSearch: 'global',
  taskDetail: 'global',
  // A file opened from the session's file tree belongs to that session, exactly
  // like the diff read from the same directory (gitDiff below).
  fileViewer: 'session',
  account: 'global',
  notification: 'global',
  // The interface language belongs to the browser profile, not to a session.
  i18n: 'global',
  sessionPreset: 'session',
  // A diff belongs to the session whose project directory it was read from.
  gitDiff: 'session',
  // The live context is read from the session's own checkpoint.
  contextViewer: 'session',
  // The trajectory ledger is per session, like the context it records.
  trajectory: 'session',
  // The browser and the terminal work on the session's own project (the
  // terminal's cwd IS that directory), so both live in its group.
  browser: 'session',
  terminal: 'session',
  // The side session was created from this session's transcript.
  sideChat: 'session'
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

/** Narrowest usable sidebar: below this the panels are cramped rather than small. */
export const RIGHT_SIDEBAR_MIN_WIDTH = 280;
/** Widest allowed sidebar: a drag can never swallow more than this (or ~half of a narrow window). */
export const RIGHT_SIDEBAR_MAX_WIDTH = 760;
/** Default width for a fresh profile. */
export const RIGHT_SIDEBAR_DEFAULT_WIDTH = 420;
/** Laying-out floor of a panel body: narrower than this it scrolls sideways instead of squeezing. */
export const RIGHT_SIDEBAR_PANEL_MIN_WIDTH = 420;
/**
 * Floor for panels whose content is a SHORT LIST rather than an editor. The
 * 420px floor exists so an 800-1600px editor body is not squeezed into a
 * column; a four-row list has no such need, and at the column's own 280px floor
 * the editor floor made it overflow its own panel with a horizontal scrollbar.
 */
export const RIGHT_SIDEBAR_NARROW_PANEL_MIN_WIDTH = 200;
/** Panel kinds that lay out inside the narrow floor (lists, not editors). */
export const NARROW_PANEL_KINDS: ReadonlySet<RightSidebarPanelKind> = new Set(['i18n']);
/** Laying-out floor of a panel body: shorter than this it scrolls vertically instead of clipping. */
export const RIGHT_SIDEBAR_PANEL_MIN_HEIGHT = 360;

/**
 * Clamp a requested width into the allowed band, also keeping the panel below
 * 65% of the viewport (ZCode's side-pane ceiling) so it can never crowd the
 * chat out on small screens.
 * @param width Requested width in px.
 * @param viewportWidth Current window width (defaults to `window.innerWidth`).
 */
export function clampSidebarWidth(
  width: number,
  viewportWidth: number = typeof window === 'undefined' ? 0 : window.innerWidth
): number {
  const viewportCap = viewportWidth > 0 ? Math.floor(viewportWidth * 0.65) : RIGHT_SIDEBAR_MAX_WIDTH;
  const cap = Math.max(RIGHT_SIDEBAR_MIN_WIDTH, Math.min(RIGHT_SIDEBAR_MAX_WIDTH, viewportCap));
  return Math.round(Math.min(cap, Math.max(RIGHT_SIDEBAR_MIN_WIDTH, width)));
}

/** Per-instance data carried by a right-sidebar tab. */
export interface RightSidebarTabPayload {
  path?: string;
  hash?: string;
  instance?: string;
  url?: string;
  /** The side session this tab talks to (one tab per session). */
  sessionId?: string;
  /** The main session this ephemeral side chat belongs to. */
  parentSessionId?: string;
  /** Whether the server inherited a parent-transcript snapshot. */
  inherited?: boolean;
  /** The quoted text the side session's composer starts with. */
  quote?: string;
  /** The quote's localized source label (the chip's tooltip). */
  source?: string;
  /**
   * Whether a `sideChat` panel prefills the quote chip on mount. Initial opens
   * default to true; reopening a closed side chat from the recent list sets
   * false, so its retained history is shown without re-inserting the quote.
   */
  prefillQuote?: boolean;
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
  /** Which group tab the tab sits under (This session / Global). */
  scope: RightSidebarScope;
  /**
   * Owning session for a SESSION-scoped tab (undefined for global tabs). The
   * strip shows only the active session's tabs; a tab from another session is
   * kept but never rendered or auto-activated.
   */
  sessionId?: string;
  /**
   * Per-instance data for kinds that need it (the file viewer's relative path).
   * The tab id is already the instance key, so the payload rides the tab itself
   * rather than a parallel path-by-tab map.
   *
   * ``instance`` is what lets TWINS coexist for one kind: the toolbox's browser
   * and terminal (and the file viewer's second file) open once per distinct
   * instance key, while the settings entries keep deduping on their empty
   * payload. The panel reads it back to key its own state.
   */
  payload?: RightSidebarTabPayload;
}

/**
 * A tab the user closed, kept so the strip's recent-tabs menu can restore it.
 * ``sessionId`` is the session it belongs to (the side chat's PARENT id for a
 * side chat); global entries use an empty id and are listed for every session.
 */
export interface RecentClosedTab {
  /** Stable identity (kind + scope + payload, minus the prefill flag). */
  key: string;
  kind: RightSidebarPanelKind;
  scope: RightSidebarScope;
  payload?: RightSidebarTabPayload;
  sessionId: string;
  closedAt: number;
}

/** Newest entries kept per recent group (per session for the session group). */
export const RECENT_CLOSED_LIMIT = 10;

/**
 * Keep the newest entries of each group and drop everything older. The session
 * group is capped PER SESSION (one session's history can never crowd out or be
 * seen by another); the global group is capped once for every session.
 * @param entries Newest-first recent entries.
 * @returns The trimmed list, newest-first.
 */
function trimRecentClosed(entries: RecentClosedTab[]): RecentClosedTab[] {
  const sessionCounts = new Map<string, number>();
  let globalCount = 0;
  const kept: RecentClosedTab[] = [];
  for (const entry of entries) {
    if (entry.scope === 'global') {
      if (globalCount >= RECENT_CLOSED_LIMIT) continue;
      globalCount += 1;
    } else {
      const count = sessionCounts.get(entry.sessionId) ?? 0;
      if (count >= RECENT_CLOSED_LIMIT) continue;
      sessionCounts.set(entry.sessionId, count + 1);
    }
    kept.push(entry);
  }
  return kept;
}

let tabSeq = 0;

/**
 * Right-sidebar state: collapsed flag (persisted, mirroring the session
 * sidebar) plus the open tab list and the active tab (in-memory — a refresh
 * starts with an empty strip).
 *
 * The sidebar lives in the home shell. GLOBAL tabs survive every session
 * switch and are shared; SESSION tabs are kept per owning session and only the
 * active session's are rendered (switching to a new session never re-displays
 * another session's tab). Panels are lazily imported by the component and
 * mounted only while active.
 */
export const useRightSidebarStore = defineStore(
  'rightSidebar',
  () => {
    /** Whether the sidebar is retracted (default: retracted, no wasted width). */
    const collapsed = ref(true);
    /** Open tabs in strip order. */
    const tabs = ref<RightSidebarTab[]>([]);
    /** Recently closed tabs (newest first), capped; persisted across reloads. */
    const recentClosed = ref<RecentClosedTab[]>([]);
    // Also trims a persisted list written by an older build with a larger cap.
    watch(
      () => recentClosed.value,
      entries => {
        const trimmed = trimRecentClosed(entries);
        if (trimmed.length !== entries.length) recentClosed.value = trimmed;
      },
      { immediate: true }
    );
    /** Active tab id, or null when nothing is open. */
    const activeTabId = ref<string | null>(null);
    /** The session whose This-session tab group is on screen ('' when on home). */
    const activeSessionId = ref('');
    /** Last active session-scoped tab per session (in-memory, restored on switch). */
    const lastActiveSessionTab = ref<Record<string, string | null>>({});
    /**
     * Which group tab is shown. Global by default: every pre-existing panel is a
     * global tool, so an untouched sidebar keeps exactly its old behaviour.
     */
    const activeScope = ref<RightSidebarScope>('global');
    /** Panel width in px (draggable, clamped; persisted). */
    const width = ref(RIGHT_SIDEBAR_DEFAULT_WIDTH);
    /**
     * Whether the panel fills the whole viewport (the strip's Zoom button).
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
     *   `hash` it was opened from), plus an `instance` key for kinds allowed to
     *   run several twins (the toolbox's browser / terminal). A browser tab may
     *   also carry the `url` it should open with (the file tree's "open in
     *   browser" hands a local page over this way).
     * @param payload.path
     * @param payload.hash
     * @param payload.instance
     * @param payload.url
     * @param payload.sessionId The side session a `sideChat` tab talks to (one
     *   tab per session; `quote`/`source` are initial content, not identity).
     * @param payload.parentSessionId The main session an ephemeral side chat
     *   belongs to (its lifecycle parent).
     * @param payload.inherited Whether the server captured the parent transcript.
     * @param payload.quote
     * @param payload.source
     * @param payload.prefillQuote
     * @returns The tab id (the existing tab's id when it was reused).
     */
    function openTab(kind: RightSidebarPanelKind, payload?: RightSidebarTabPayload): string {
      const path = payload?.path ?? null;
      const hash = payload?.hash ?? null;
      const instance = payload?.instance ?? null;
      const url = payload?.url ?? null;
      const sessionId = payload?.sessionId ?? null;
      const scope = SCOPE_BY_KIND[kind];
      // A side-chat tab belongs to its PARENT session; every other session tab
      // belongs to the session on screen when it was opened.
      const ownerSessionId =
        scope === 'session' ? String(payload?.parentSessionId || activeSessionId.value || '') : undefined;
      const existing = tabs.value.find(
        tab =>
          tab.kind === kind &&
          (tab.sessionId ?? null) === (ownerSessionId ?? null) &&
          (tab.payload?.path ?? null) === path &&
          (tab.payload?.hash ?? null) === hash &&
          (tab.payload?.instance ?? null) === instance &&
          (tab.payload?.url ?? null) === url &&
          (tab.payload?.sessionId ?? null) === sessionId
      );
      if (existing) {
        activeTabId.value = existing.id;
        activeScope.value = existing.scope;
        if (existing.scope === 'session' && existing.sessionId !== undefined) {
          lastActiveSessionTab.value[existing.sessionId] = existing.id;
        }
        expand();
        if (WIDE_PANEL_KINDS.has(kind)) setWidth(Math.max(width.value, RIGHT_SIDEBAR_WIDE_PANEL_WIDTH));
        return existing.id;
      }
      const id = `${kind}-${++tabSeq}`;
      const tab: RightSidebarTab = {
        id,
        kind,
        scope,
        ...(ownerSessionId !== undefined ? { sessionId: ownerSessionId } : {}),
        ...(payload ? { payload } : {})
      };
      tabs.value = [...tabs.value, tab];
      activeTabId.value = id;
      activeScope.value = scope;
      if (ownerSessionId !== undefined) lastActiveSessionTab.value[ownerSessionId] = id;
      expand();
      if (WIDE_PANEL_KINDS.has(kind)) setWidth(Math.max(width.value, RIGHT_SIDEBAR_WIDE_PANEL_WIDTH));
      return id;
    }

    /**
     * Tabs of one group (the strip renders the active group's list).
     *
     * The session group is filtered to the ACTIVE session: a tab opened in
     * another session stays in the store but is never shown or activated here.
     * @param scope Group to list.
     * @returns The visible tabs of that scope, in strip order.
     */
    function tabsInScope(scope: RightSidebarScope): RightSidebarTab[] {
      return tabs.value.filter(
        tab => tab.scope === scope && (scope === 'global' || tab.sessionId === activeSessionId.value)
      );
    }

    /**
     * Switch the session whose This-session tab group is shown.
     *
     * GLOBAL tabs are shared and stay active. When the group on screen is the
     * session group, it switches to that session's last active tab (or clears
     * the body when the session has no session tabs) — another session's tab is
     * never carried over.
     * @param sessionId New active session id ('' on home).
     */
    function setActiveSession(sessionId: string): void {
      const next = sessionId || '';
      if (activeSessionId.value === next) return;
      activeSessionId.value = next;
      if (activeScope.value === 'global') return;
      const remembered = lastActiveSessionTab.value[next] ?? null;
      const tab = remembered
        ? tabs.value.find(
            candidate => candidate.id === remembered && candidate.scope === 'session' && candidate.sessionId === next
          )
        : undefined;
      activeTabId.value = tab?.id ?? null;
    }

    /**
     * Show a group tab. When the active tab lives in the OTHER group the group's
     * first tab is activated, so the body always belongs to the group on screen
     * (an empty group shows its empty state instead).
     * @param scope Group to show.
     */
    function setActiveScope(scope: RightSidebarScope): void {
      activeScope.value = scope;
      const visible = tabsInScope(scope);
      const active = visible.find(tab => tab.id === activeTabId.value);
      if (active) return;
      const remembered = scope === 'session' ? (lastActiveSessionTab.value[activeSessionId.value] ?? null) : null;
      const next = (remembered ? visible.find(tab => tab.id === remembered) : undefined) ?? visible[0];
      activeTabId.value = next?.id ?? null;
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
      if (tab.scope === 'session' && tab.sessionId !== undefined) {
        lastActiveSessionTab.value[tab.sessionId] = id;
      }
    }

    /**
     * Close a tab; the neighbour to its right (else the left) becomes active,
     * and the last tab closing leaves no active tab (the empty state shows).
     * @param id Tab id.
     */
    function closeTab(id: string): void {
      const index = tabs.value.findIndex(tab => tab.id === id);
      if (index === -1) return;
      const closing = tabs.value[index];
      if (!closing) return;
      // The neighbour must come from the SAME visible group: a session tab's
      // sibling is another tab of that session, never a hidden tab of another
      // session or a global one.
      const group = tabs.value.filter(
        tab => tab.scope === closing.scope && (tab.scope === 'global' || tab.sessionId === closing.sessionId)
      );
      const groupIndex = group.findIndex(tab => tab.id === id);
      const remainingGroup = group.filter(tab => tab.id !== id);
      const sameGroupNeighbour = remainingGroup[groupIndex] ?? remainingGroup[groupIndex - 1] ?? null;
      const remaining = [...tabs.value];
      remaining.splice(index, 1);
      tabs.value = remaining;
      // With no sibling left, fall back to the other group's first visible tab
      // (closing the last session tab shows a global one, as before) — never to
      // a hidden tab of another session.
      const fallback =
        sameGroupNeighbour ??
        (closing.scope === 'session'
          ? remaining.find(tab => tab.scope === 'global')
          : remaining.find(tab => tab.scope === 'session' && tab.sessionId === activeSessionId.value)) ??
        null;
      if (closing.scope === 'session' && closing.sessionId !== undefined) {
        if (lastActiveSessionTab.value[closing.sessionId] === id) {
          lastActiveSessionTab.value[closing.sessionId] = sameGroupNeighbour?.id ?? null;
        }
      }
      if (activeTabId.value === id) {
        activeTabId.value = fallback?.id ?? null;
        if (fallback) activeScope.value = fallback.scope;
      }
    }

    /**
     * Remember a tab the user just closed, for the strip's recent-tabs menu.
     *
     * The entry keeps the tab's kind/scope/payload (so reopening restores the
     * same panel) and the session it belongs to: a side chat uses its PARENT
     * session id, other session-scoped tabs use the session that was active at
     * close. Re-closing the same tab moves it to the front instead of stacking a
     * duplicate; each group keeps only its newest {@link RECENT_CLOSED_LIMIT}
     * pages (older entries are dropped). The list is persisted.
     * @param tab The tab being closed.
     * @param activeSessionId The session shown when it was closed.
     */
    function rememberClosed(tab: RightSidebarTab, activeSessionId?: string): void {
      const payload = tab.payload ? { ...tab.payload } : undefined;
      const identity = payload
        ? Object.fromEntries(
            Object.entries(payload)
              .filter(([key]) => key !== 'prefillQuote')
              .sort(([left], [right]) => left.localeCompare(right))
          )
        : null;
      const key = `${tab.kind}|${tab.scope}|${JSON.stringify(identity)}`;
      const sessionId = tab.scope === 'session' ? String(tab.sessionId || activeSessionId || '') : '';
      const entry: RecentClosedTab = {
        key,
        kind: tab.kind,
        scope: tab.scope,
        payload,
        sessionId,
        closedAt: Date.now()
      };
      recentClosed.value = trimRecentClosed([entry, ...recentClosed.value.filter(candidate => candidate.key !== key)]);
    }

    /**
     * Closed tabs belonging to one group, newest first. The session group only
     * returns entries of the CURRENT session; the global group returns all.
     * @param scope Group to list.
     * @param sessionId Current session id.
     */
    function recentClosedFor(scope: RightSidebarScope, sessionId: string): RecentClosedTab[] {
      const entries =
        scope === 'global'
          ? recentClosed.value.filter(entry => entry.scope === 'global')
          : recentClosed.value.filter(entry => entry.scope === 'session' && entry.sessionId === sessionId);
      return entries.slice(0, RECENT_CLOSED_LIMIT);
    }

    /**
     * Reopen a recently closed tab and remove it from the recent list. A side
     * chat is restored with ``prefillQuote: false``: its retained history is
     * the point, so the original quote chip is not inserted again.
     * @param entry The recent entry to restore.
     * @returns The new tab id.
     */
    function reopenClosed(entry: RecentClosedTab): string {
      const payload =
        entry.kind === 'sideChat' && entry.payload ? { ...entry.payload, prefillQuote: false } : entry.payload;
      const id = openTab(entry.kind, payload);
      recentClosed.value = recentClosed.value.filter(candidate => candidate.key !== entry.key);
      return id;
    }

    /**
     * Drop everything a deleted session owned: its recent-tabs entries, its
     * session-scoped open tabs (including a side-chat panel pointing at it) and
     * its remembered active tab.
     * @param sessionId Deleted session id.
     */
    function forgetSession(sessionId: string): void {
      if (!sessionId) return;
      recentClosed.value = recentClosed.value.filter(
        entry => entry.sessionId !== sessionId && entry.payload?.sessionId !== sessionId
      );
      const removedIds = new Set(
        tabs.value
          .filter(
            tab => tab.sessionId === sessionId || (tab.scope === 'session' && tab.payload?.sessionId === sessionId)
          )
          .map(tab => tab.id)
      );
      if (removedIds.size > 0) {
        tabs.value = tabs.value.filter(tab => !removedIds.has(tab.id));
        if (activeTabId.value && removedIds.has(activeTabId.value)) {
          activeTabId.value = null;
        }
      }
      delete lastActiveSessionTab.value[sessionId];
      if (activeSessionId.value === sessionId) activeSessionId.value = '';
    }

    return {
      collapsed,
      tabs,
      recentClosed,
      activeTabId,
      activeSessionId,
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
      setActiveSession,
      setActiveScope,
      activateTab,
      closeTab,
      rememberClosed,
      recentClosedFor,
      reopenClosed,
      forgetSession
    };
  },
  {
    persist: {
      pick: ['collapsed', 'width', 'maximized', 'recentClosed']
    }
  }
);
