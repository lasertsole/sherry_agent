/**
 * Project file browser bridge calls (`/project/tree` + `/project/file`) and the
 * system folder picker (`/system/dirs`).
 *
 * The tree calls are session-scoped like every other session API: the backend
 * resolves the paths inside that session's project directory only. The folder
 * picker deliberately is not — choosing a NEW project root starts outside the
 * current one — and answers the directory names of one absolute path, nothing
 * else (no files, no contents).
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
  // `fetchApiPayload` passes a failed request's null payload through unchanged;
  // surface it as an error instead of throwing on the first property access.
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
  if (!res) throw new Error('project tree request failed');
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
  if (!res) throw new Error('project file request failed');
  return { path, content: res.content ?? '', size: res.size ?? 0 };
}

/** One subdirectory of a system folder level. */
export interface SystemDirEntry {
  name: string;
  /** Absolute path of the child (what the picker binds when confirmed). */
  path: string;
}

/** Response of `GET /system/dirs`. */
export interface SystemDirLevel {
  /** Absolute, symlink-resolved path of the level. */
  path: string;
  /** Parent directory, or null at the filesystem root (the "go up" stop). */
  parent: string | null;
  entries: SystemDirEntry[];
  truncated: boolean;
  total: number;
}

/**
 * List the subdirectories of one absolute path (the project-directory picker).
 *
 * @param path Absolute directory; '' starts at the server user's home.
 */
export async function fetchSystemDirs(path = ''): Promise<SystemDirLevel> {
  const res = await fetchApiPayload<{
    path?: string;
    parent?: string | null;
    entries?: SystemDirEntry[];
    truncated?: boolean;
    total?: number;
  }>({
    url: '/system/dirs',
    opts: { path },
    method: 'get'
  });
  if (!res) throw new Error('system folder request failed');
  return {
    path: res.path ?? path,
    parent: res.parent ?? null,
    entries: res.entries ?? [],
    truncated: res.truncated === true,
    total: res.total ?? (res.entries ?? []).length
  };
}
