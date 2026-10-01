import { defineStore } from 'pinia';
import type { ProjectDirectoryState } from '~/composables/bridge/session';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchProjectDirectory, setProjectDirectory } from '~/composables/bridge/session';

/** One session's project-directory view (absent until the first read). */
export interface ProjectDirectoryEntry extends ProjectDirectoryState {
  /** Last write/rejection text, shown in the popover (null = none). */
  error: string | null;
}

/** Unbound session running on the process default. */
const EMPTY: ProjectDirectoryEntry = {
  directory: null,
  effective: '',
  source: 'default',
  pendingDirectory: null,
  error: null
};

/**
 * Per-session project directory (toolbar folder chip).
 *
 * The backend owns the truth: the value lives in the session state register
 * every tool reads on each call, so a switch applies from the next turn on
 * (while a turn is in flight the choice is parked and the chip shows the clock
 * icon until the turn boundary promotes it).
 */
export const useProjectDirectoryStore = defineStore('projectDirectory', () => {
  const bySession = ref<Record<string, ProjectDirectoryEntry>>({});

  /**
   * The session's state, defaulting to "unbound, process default".
   * @param sessionId
   */
  function stateFor(sessionId: string): ProjectDirectoryEntry {
    return bySession.value[sessionId] ?? EMPTY;
  }

  function _write(sessionId: string, entry: ProjectDirectoryEntry): void {
    bySession.value = { ...bySession.value, [sessionId]: entry };
  }

  /**
   * Pull the session's state from the backend (a failed read keeps the last one).
   * @param sessionId
   */
  async function hydrate(sessionId: string): Promise<void> {
    if (!sessionId) return;
    try {
      const state = await fetchProjectDirectory(sessionId);
      _write(sessionId, { ...state, error: null });
    } catch {
      // Keep what we have; the next hydrate retries.
    }
  }

  /**
   * Bind (or clear, with ``null``) the session's directory.
   *
   * Optimistic like the model picker: the chip updates immediately, the
   * backend's answer corrects it, and a rejection rolls back with the reason
   * kept for the popover (the server is the only authority on validity).
   * @param sessionId
   * @param directory
   */
  async function select(sessionId: string, directory: string | null): Promise<void> {
    if (!sessionId) return;
    const previous = stateFor(sessionId);
    _write(sessionId, {
      ...previous,
      // A parked choice does NOT overwrite the live value: the running turn
      // keeps resolving against the old root until the boundary.
      directory: previous.directory,
      pendingDirectory: directory,
      error: null
    });
    try {
      const applied = await setProjectDirectory(sessionId, directory);
      _write(sessionId, { ...applied.state, error: applied.ok ? null : previous.error });
    } catch (e) {
      _write(sessionId, {
        ...previous,
        error: e instanceof Error ? e.message : String(e)
      });
    }
  }

  /**
   * Record a client-side validation failure (nothing was sent).
   * @param sessionId
   * @param message
   */
  function fail(sessionId: string, message: string): void {
    _write(sessionId, { ...stateFor(sessionId), error: message });
  }

  /**
   * Drop the recorded error (popover re-opened / next attempt).
   * @param sessionId
   */
  function clearError(sessionId: string): void {
    const current = bySession.value[sessionId];
    if (current?.error) _write(sessionId, { ...current, error: null });
  }

  return { bySession, stateFor, hydrate, select, fail, clearError };
});
