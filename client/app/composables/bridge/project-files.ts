/**
 * Project file browser bridge calls (`/project/tree` + `/project/file`).
 *
 * Session-scoped like every other session API: the backend resolves the paths
 * inside that session's project directory only.
 *
 * @module bridge/project-files
 */

/** One entry of a directory level. */
export interface ProjectTreeEntry {
  name: string;
  type: 'dir' | 'file';
  /** Byte size (files only). */
  size?: number;
}

/** Response of `GET /project/tree`. */
export interface ProjectTreeLevel {
  sessionId: string;
  /** Absolute project root the level belongs to. */
  root: string;
  /** Relative path of this level (empty = the root). */
  path: string;
  entries: ProjectTreeEntry[];
  /** Whether entries were cut at the configured per-level cap. */
  truncated: boolean;
  /** Entries the level actually held before truncation. */
  total: number;
}

/** Response of `GET /project/file`. */
export interface ProjectFileContent {
  path: string;
  content: string;
  size: number;
}

/**
 * List one directory level of the session's project tree.
 *
 * @param sessionId Session whose project root should be listed.
 * @param path Relative subdirectory ('' = the root).
 */
export async function fetchProjectTree(sessionId: string, path = ''): Promise<ProjectTreeLevel> {
  const res = await fetchApiPayload<{
    root?: string;
    path?: string;
    entries?: ProjectTreeEntry[];
    truncated?: boolean;
    total?: number;
  }>({
    url: '/project/tree',
    opts: { session_id: sessionId, path },
    method: 'get'
  });
  return {
    sessionId,
    root: res.root ?? '',
    path: res.path ?? path,
    entries: res.entries ?? [],
    truncated: res.truncated === true,
    total: res.total ?? (res.entries ?? []).length
  };
}

/**
 * Read one file preview.
 *
 * Rejections carry the backend's `reason` (escapes, size, binary) — the caller
 * shows it verbatim.
 *
 * @param sessionId Session whose project root should be read.
 * @param path Relative file path.
 */
export async function fetchProjectFile(sessionId: string, path: string): Promise<ProjectFileContent> {
  const res = await fetchApiPayload<{ content?: string; size?: number }>({
    url: '/project/file',
    opts: { session_id: sessionId, path },
    method: 'get'
  });
  return { path, content: res.content ?? '', size: res.size ?? 0 };
}
