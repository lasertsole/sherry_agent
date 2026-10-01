import { defineStore } from 'pinia';

/** One cached file preview. */
export interface FileViewerEntry {
  content: string;
  size: number;
  fetchedAt: number;
}

/** Cap on cached previews (the sidebar has no KeepAlive, so this is the cache). */
const MAX_ENTRIES = 20;

/**
 * Content cache for the file viewer panel.
 *
 * `RightSidebar` deliberately has no KeepAlive (unmounting a panel closes its
 * live streams), so switching tabs away and back would re-fetch the file. The
 * cache keeps the last N previews per session key: a mount renders from cache
 * immediately and only fetches on a miss. Entries are never refreshed in the
 * background — the user wants the version they just looked at.
 */
export const useFileViewerStore = defineStore('fileViewer', () => {
  /** ``sessionId::path`` → preview. */
  const entries = ref<Record<string, FileViewerEntry>>({});

  /**
   * Cache key for one file of one session.
   * @param sessionId
   * @param path
   */
  function keyFor(sessionId: string, path: string): string {
    return `${sessionId}::${path}`;
  }

  /**
   * The cached preview, or null on a miss.
   * @param sessionId
   * @param path
   */
  function get(sessionId: string, path: string): FileViewerEntry | null {
    return entries.value[keyFor(sessionId, path)] ?? null;
  }

  /**
   * Store one preview, evicting the oldest entries beyond the cap.
   * @param sessionId
   * @param path
   * @param content
   * @param size
   */
  function put(sessionId: string, path: string, content: string, size: number): void {
    const next: Record<string, FileViewerEntry> = {
      ...entries.value,
      [keyFor(sessionId, path)]: { content, size, fetchedAt: Date.now() }
    };
    const keys = Object.keys(next);
    if (keys.length > MAX_ENTRIES) {
      const oldest = keys
        .sort((a, b) => (next[a]?.fetchedAt ?? 0) - (next[b]?.fetchedAt ?? 0))
        .slice(0, keys.length - MAX_ENTRIES);
      for (const stale of oldest) delete next[stale];
    }
    entries.value = next;
  }

  /** Drop every cached preview (a project switch invalidates relative paths). */
  function clear(): void {
    entries.value = {};
  }

  return { entries, get, put, clear };
});
