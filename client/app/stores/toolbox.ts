import { defineStore } from 'pinia';

/**
 * Toolbox state: the browser panel's URL history and the user terminal's log,
 * both kept PER SESSION.
 *
 * They live in a store rather than in the components because the right sidebar
 * unmounts a panel when its tab is not active (no KeepAlive, on purpose — that
 * is what tears a panel's streams down): a terminal whose scrollback vanished on
 * every tab switch would be unusable.
 */

/** One browser tab's navigation state (per session). */
export interface BrowserState {
  /** The current page URL (empty until the user types one). */
  url: string;
  /** Visited URLs, oldest first (back/forward walk this list). */
  history: string[];
  /** Index inside `history` (the current page). */
  index: number;
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

const EMPTY_BROWSER: BrowserState = { url: '', history: [], index: -1 };

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
   * The session's browser state (created on first use).
   * @param sessionId
   */
  function browserFor(sessionId: string): BrowserState {
    return browser.value[sessionId] ?? EMPTY_BROWSER;
  }

  /**
   * Navigate to an address (dropping any forward history, like a browser).
   * @param sessionId
   * @param input Raw address-bar text.
   */
  function navigate(sessionId: string, input: string): void {
    const url = normalizeUrl(input);
    if (!url) return;
    const current = browserFor(sessionId);
    const history = [...current.history.slice(0, current.index + 1), url];
    browser.value = { ...browser.value, [sessionId]: { url, history, index: history.length - 1 } };
  }

  /**
   * Step through the visited history.
   * @param sessionId
   * @param stepValue -1 for back, +1 for forward.
   */
  function step(sessionId: string, stepValue: -1 | 1): void {
    const current = browserFor(sessionId);
    const next = current.index + stepValue;
    if (next < 0 || next >= current.history.length) return;
    browser.value = {
      ...browser.value,
      [sessionId]: { url: current.history[next] ?? '', history: current.history, index: next }
    };
  }

  /**
   * Whether the session can go back / forward (the toolbar's disabled state).
   * @param sessionId
   */
  const canGoBack = (sessionId: string): boolean => browserFor(sessionId).index > 0;
  const canGoForward = (sessionId: string): boolean => {
    const state = browserFor(sessionId);
    return state.index >= 0 && state.index < state.history.length - 1;
  };

  /**
   * The session's terminal scrollback.
   * @param sessionId
   */
  function terminalFor(sessionId: string): TerminalEntry[] {
    return terminal.value[sessionId] ?? [];
  }

  /**
   * Append one finished run and remember the directory it ran in.
   * @param sessionId
   * @param entry The run's result.
   */
  function recordRun(sessionId: string, entry: TerminalEntry): void {
    terminal.value = {
      ...terminal.value,
      [sessionId]: [...terminalFor(sessionId), entry]
    };
    terminalCwd.value = { ...terminalCwd.value, [sessionId]: entry.cwd };
  }

  /**
   * Remember the prompt directory (read once per panel open, or after a run).
   * @param sessionId
   * @param cwd
   */
  function setTerminalCwd(sessionId: string, cwd: string): void {
    if (!cwd) return;
    terminalCwd.value = { ...terminalCwd.value, [sessionId]: cwd };
  }

  /**
   * Forget the session's scrollback (the panel's 清空 button).
   * @param sessionId
   */
  function clearTerminal(sessionId: string): void {
    terminal.value = { ...terminal.value, [sessionId]: [] };
  }

  return {
    browser,
    terminal,
    terminalCwd,
    browserFor,
    navigate,
    step,
    canGoBack,
    canGoForward,
    terminalFor,
    recordRun,
    setTerminalCwd,
    clearTerminal
  };
});
