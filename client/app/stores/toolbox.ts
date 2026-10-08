import { defineStore } from 'pinia';

/**
 * Toolbox state: the browser panel's URL history and the user terminal's log,
 * kept per PANEL INSTANCE (a ``sessionId::instance`` key — the toolbox opens
 * several browsers / terminals per session).
 *
 * They live in a store rather than in the components because the right sidebar
 * unmounts a panel when its tab is not active (no KeepAlive, on purpose — that
 * is what tears a panel's streams down): a terminal whose scrollback vanished on
 * every tab switch would be unusable.
 */

/** The emulated frame of one browser instance (free-size mode). */
export interface BrowserViewport {
  /** Width in CSS px. */
  width: number;
  /** Height in CSS px. */
  height: number;
}

/** One browser tab's navigation + free-size state (per instance). */
export interface BrowserState {
  /** The current page URL (empty until the user types one). */
  url: string;
  /** Visited URLs, oldest first (back/forward walk this list). */
  history: string[];
  /** Index inside `history` (the current page). */
  index: number;
  /** Free-size mode: the page renders in a fixed emulated frame the user resizes. */
  responsive: boolean;
  /** That frame's size (only meaningful while `responsive`). */
  viewport: BrowserViewport;
  /** How that frame is scaled: fit to the panel, or a fixed percentage. */
  zoom: BrowserZoom;
}

/**
 * Free-size bounds, mirroring ZCode's ``BROWSER_VIEWPORT_LIMITS``: its device
 * emulation and its agent-side ``setViewportSize`` share one safe band, and so
 * does this panel (the same numbers, so a size means the same thing in both).
 */
export const BROWSER_VIEWPORT_LIMITS = {
  minWidth: 320,
  maxWidth: 3840,
  minHeight: 320,
  maxHeight: 2160
} as const;

/** The frame a browser instance starts free-size mode with (ZCode's default). */
export const DEFAULT_BROWSER_VIEWPORT: BrowserViewport = { width: 393, height: 852 };

/** How the emulated frame is scaled: to fit the panel, or a fixed percentage. */
export type BrowserZoom = 'fit' | '50' | '75' | '100' | '125' | '150' | '200';

/** The zoom options the size row offers (ZCode's own set, fit first). */
export const BROWSER_ZOOM_OPTIONS: readonly BrowserZoom[] = ['fit', '50', '75', '100', '125', '150', '200'] as const;

/** Fit is the default: a phone-sized frame rarely fits a sidebar unchanged. */
export const DEFAULT_BROWSER_ZOOM: BrowserZoom = 'fit';

/**
 * The scale a zoom means: ``null`` for fit (the caller computes it).
 * @param zoom
 */
export function browserZoomScale(zoom: BrowserZoom): number | null {
  return zoom === 'fit' ? null : Number(zoom) / 100;
}

/**
 * Clamp a requested frame size into :data:`BROWSER_VIEWPORT_LIMITS`.
 * @param viewport Requested size in CSS px.
 */
export function clampBrowserViewport(viewport: BrowserViewport): BrowserViewport {
  const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, Math.round(value)));
  return {
    width: clamp(viewport.width, BROWSER_VIEWPORT_LIMITS.minWidth, BROWSER_VIEWPORT_LIMITS.maxWidth),
    height: clamp(viewport.height, BROWSER_VIEWPORT_LIMITS.minHeight, BROWSER_VIEWPORT_LIMITS.maxHeight)
  };
}

/** One finished command run (the terminal's scrollback entry). */
export interface TerminalEntry {
  command: string;
  output: string;
  exitCode: number;
  durationMs: number;
  truncated: boolean;
  /** Which directory it ran in (the session's project directory at the time). */
  cwd: string;
}

const EMPTY_BROWSER: BrowserState = {
  url: '',
  history: [],
  index: -1,
  responsive: false,
  viewport: { ...DEFAULT_BROWSER_VIEWPORT },
  zoom: DEFAULT_BROWSER_ZOOM
};

/**
 * Turn what the user typed into a URL: a bare host gets ``https://``, anything
 * that already carries a scheme (or a leading slash / dot) is left alone.
 * @param input Raw address-bar text.
 */
export function normalizeUrl(input: string): string {
  const text = input.trim();
  if (!text) return '';
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text) || text.startsWith('/') || text.startsWith('.')) {
    return text;
  }
  return `https://${text}`;
}

export const useToolboxStore = defineStore('toolbox', () => {
  const browser = ref<Record<string, BrowserState>>({});
  const terminal = ref<Record<string, TerminalEntry[]>>({});
  /** The directory each session's terminal last ran in (its prompt). */
  const terminalCwd = ref<Record<string, string>>({});

  /**
   * One browser panel's state (created on first use).
   * @param key The panel's identity (``sessionId::instance``).
   */
  function browserFor(key: string): BrowserState {
    return browser.value[key] ?? EMPTY_BROWSER;
  }

  /**
   * Navigate to an address (dropping any forward history, like a browser).
   * @param key The panel's identity.
   * @param input Raw address-bar text.
   */
  function navigate(key: string, input: string): void {
    const url = normalizeUrl(input);
    if (!url) return;
    const current = browserFor(key);
    const history = [...current.history.slice(0, current.index + 1), url];
    browser.value = {
      ...browser.value,
      [key]: { ...current, url, history, index: history.length - 1 }
    };
  }

  /**
   * Step through the visited history.
   * @param key The panel's identity.
   * @param stepValue -1 for back, +1 for forward.
   */
  function step(key: string, stepValue: -1 | 1): void {
    const current = browserFor(key);
    const next = current.index + stepValue;
    if (next < 0 || next >= current.history.length) return;
    browser.value = {
      ...browser.value,
      [key]: { ...current, url: current.history[next] ?? '', index: next }
    };
  }

  /**
   * Whether the session can go back / forward (the toolbar's disabled state).
   * @param key The panel's identity.
   */
  const canGoBack = (key: string): boolean => browserFor(key).index > 0;
  const canGoForward = (key: string): boolean => {
    const state = browserFor(key);
    return state.index >= 0 && state.index < state.history.length - 1;
  };

  /**
   * The session's terminal scrollback.
   * @param key The panel's identity.
   */
  function terminalFor(key: string): TerminalEntry[] {
    return terminal.value[key] ?? [];
  }

  /**
   * Append one finished run and remember the directory it ran in.
   * @param key The panel's identity.
   * @param entry The run's result.
   */
  function recordRun(key: string, entry: TerminalEntry): void {
    terminal.value = {
      ...terminal.value,
      [key]: [...terminalFor(key), entry]
    };
    terminalCwd.value = { ...terminalCwd.value, [key]: entry.cwd };
  }

  /**
   * Remember the prompt directory (read once per panel open, or after a run).
   * @param key The panel's identity.
   * @param cwd
   */
  function setTerminalCwd(key: string, cwd: string): void {
    if (!cwd) return;
    terminalCwd.value = { ...terminalCwd.value, [key]: cwd };
  }

  /**
   * Toggle free-size mode for one browser instance (the device-frame button).
   * Entering it keeps the size that instance last used.
   * @param key The panel's identity.
   * @param enabled
   */
  function setBrowserResponsive(key: string, enabled: boolean): void {
    const current = browserFor(key);
    browser.value = { ...browser.value, [key]: { ...current, responsive: enabled } };
  }

  /**
   * Set the emulated frame's size (the drag handles / keyboard), clamped.
   * @param key The panel's identity.
   * @param viewport Requested size in CSS px.
   */
  function setBrowserViewport(key: string, viewport: BrowserViewport): void {
    const current = browserFor(key);
    browser.value = {
      ...browser.value,
      [key]: { ...current, viewport: clampBrowserViewport(viewport) }
    };
  }

  /**
   * Set how the emulated frame is scaled (the size row's picker).
   * @param key The panel's identity.
   * @param zoom `fit` or a percentage from :data:`BROWSER_ZOOM_OPTIONS`.
   */
  function setBrowserZoom(key: string, zoom: BrowserZoom): void {
    const current = browserFor(key);
    browser.value = { ...browser.value, [key]: { ...current, zoom } };
  }

  /**
   * Forget the session's scrollback (the panel's 清空 button).
   * @param key The panel's identity.
   */
  function clearTerminal(key: string): void {
    terminal.value = { ...terminal.value, [key]: [] };
  }

  return {
    browser,
    terminal,
    terminalCwd,
    browserFor,
    navigate,
    step,
    setBrowserResponsive,
    setBrowserViewport,
    setBrowserZoom,
    canGoBack,
    canGoForward,
    terminalFor,
    recordRun,
    setTerminalCwd,
    clearTerminal
  };
});
