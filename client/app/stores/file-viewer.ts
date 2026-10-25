import { defineStore } from 'pinia';

/**
 * One cached preview without its fetch timestamp — the shape `put` accepts
 * (written out per-member: `Omit` over a union collapses to the common keys).
 */
export type FileViewerEntryInput =
  { kind: 'text'; content: string; size: number } | { kind: 'blob'; bytes: ArrayBuffer; mime: string; size: number };

/** One cached file preview: a text decode (the markdown / `<pre>` branches) or
 * the raw bytes of a rendered document (PDF / docx / image) with its mime. */
export type FileViewerEntry = FileViewerEntryInput & { fetchedAt: number };

/** Cap on cached previews per count (the sidebar has no KeepAlive, so this is the cache). */
const MAX_ENTRIES = 20;

/**
 * Cap on the bytes of cached blobs. Text entries are tiny, but a 20-entry cap
 * of 20 MiB PDFs could pin hundreds of MB — binary entries evict by weight,
 * oldest first, until under budget. Exported for the store's tests.
 */
export const MAX_BLOB_CACHE_BYTES_FOR_TEST = 64 * 1024 * 1024;
const MAX_BLOB_CACHE_BYTES = MAX_BLOB_CACHE_BYTES_FOR_TEST;

/**
 * Content cache for the file viewer panel.
 *
 * `RightSidebar` deliberately has no KeepAlive (unmounting a panel closes its
 * live streams), so switching tabs away and back would re-fetch the file. The
 * cache keeps the last N previews per session key: a mount renders from cache
 * immediately and only fetches on a miss. Entries are never refreshed in the
 * background — the user wants the version they just looked at. Blob entries
 * (cached as ArrayBuffer, never as an object URL — those belong to the
 * component lifecycle) additionally obey the byte budget.
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
   * Drop every cached preview (a project switch invalidates relative paths).
   */
  function clear(): void {
    entries.value = {};
  }

  /**
   * Store one preview, evicting beyond the caps: 20 entries by age, and blob
   * entries by total bytes.
   *
   * Two shapes: the legacy text call `put(sid, path, content, size)` and the
   * blob call with one entry object (`kind` selects the cache shape).
   * @param sessionId
   * @param path
   * @param content The decoded text (legacy text shape).
   * @param size Byte size of the source file.
   */
  function put(sessionId: string, path: string, content: string, size: number): void;
  function put(sessionId: string, path: string, entry: FileViewerEntryInput): void;
  function put(sessionId: string, path: string, contentOrEntry: string | FileViewerEntryInput, size?: number): void {
    const entry: FileViewerEntryInput =
      typeof contentOrEntry === 'string'
        ? { kind: 'text', content: contentOrEntry, size: size ?? contentOrEntry.length }
        : contentOrEntry;
    const next: Record<string, FileViewerEntry> = {
      ...entries.value,
      [keyFor(sessionId, path)]: { ...entry, fetchedAt: Date.now() } as FileViewerEntry
    };
    const keys = Object.keys(next);
    if (keys.length > MAX_ENTRIES) {
      const oldest = keys
        .sort((a, b) => (next[a]?.fetchedAt ?? 0) - (next[b]?.fetchedAt ?? 0))
        .slice(0, keys.length - MAX_ENTRIES);
      for (const stale of oldest) delete next[stale];
    }
    // Blob byte budget: evict oldest-first until the binaries fit. Text
    // entries weigh nothing (they are the legacy shape and stay tiny).
    let blobBytes = Object.values(next).reduce((sum, entry) => (entry.kind === 'blob' ? sum + entry.size : sum), 0);
    if (blobBytes > MAX_BLOB_CACHE_BYTES) {
      const byAge = Object.keys(next).sort((a, b) => (next[a]?.fetchedAt ?? 0) - (next[b]?.fetchedAt ?? 0));
      for (const key of byAge) {
        if (blobBytes <= MAX_BLOB_CACHE_BYTES) break;
        const entry = next[key];
        if (entry?.kind !== 'blob') continue;
        delete next[key];
        blobBytes -= entry.size;
      }
    }
    entries.value = next;
  }

  return { entries, get, put, clear };
});
