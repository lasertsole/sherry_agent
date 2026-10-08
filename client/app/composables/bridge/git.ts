/**
 * Git-graph bridge (the left sidebar's history panel).
 *
 * One call: {@link fetchGitGraph} mirrors `GET /git/graph` — one page of the
 * session project's history plus the header facts (branch, dirty count, the
 * local branches). The backend answers `available: false` with a reason
 * (`not-a-repository` / `git-unavailable`) instead of an error, so the panel can
 * show its own empty state without a toast.
 *
 * EVERY failure THROWS — the two write actions (`reset` / `checkout`) go through
 * the raw transport so a refusal's server-side `reason` (git's own stderr, e.g.
 * "Your local changes would be overwritten by checkout") reaches the caller's
 * toast, and the reads reject instead of handing back a null payload: a null
 * assigned to the panel's `page` used to brick its render entirely.
 *
 * @module bridge/git
 */
// ``fetchApiPayload`` is auto-imported from ~/composables/requestApi (the bridge
// convention: no explicit import of an auto-imported symbol); the raw transport
// is imported explicitly, as in bridge/auth.ts.
import { fetchApiRaw } from '../requestApi';

/** One ref chip on a commit row (head / branch / tag / remote). */
export interface GitRefEntry {
  kind: 'head' | 'branch' | 'tag' | 'remote' | string;
  name: string;
}

/** One commit row of the graph. */
export interface GitCommitEntry {
  hash: string;
  /** First 8 characters of the hash — what the row shows. */
  short: string;
  /** Parent hashes, first-parent first (the lane layout follows the order). */
  parents: string[];
  author: string;
  /** ISO-8601 author date. */
  date: string;
  refs: GitRefEntry[];
  /** First line of the commit message, bounded by the backend. */
  subject: string;
}

export interface GitGraphPage {
  /** Absolute project root the repository was read from. */
  root: string;
  /** How that root was resolved: `session` / `env` / `default`. */
  source: string;
  /** False for a directory that is not a usable repository (see `reason`). */
  available: boolean;
  /** `not-a-repository` / `git-unavailable` (empty when available). */
  reason: string;
  /** Current branch name (`HEAD` when detached; empty for an empty repository). */
  branch: string;
  detached: boolean;
  /** Uncommitted entries (tracked + untracked), capped by the backend. */
  dirty: number;
  /** True when the dirty count hit its cap (the number is a floor then). */
  dirty_capped: boolean;
  commits: GitCommitEntry[];
  /** True when another page exists. */
  hasMore: boolean;
  /** Local branches, most recently committed first (the branch switcher's list). */
  branches: string[];
}

/**
 * Read one page of the session project's git history.
 * @param sessionId Session whose project directory is read.
 * @param options Paging (`limit` commits from `skip`).
 * @param options.limit
 * @param options.skip
 */
/**
 * One response payload → the page shape (the read route and both write actions
 * answer the same JSON).
 * @param res Parsed payload from the backend.
 */
function toPage(res: Record<string, unknown> & { success?: boolean }): GitGraphPage {
  return {
    root: String(res.root ?? ''),
    source: String(res.source ?? ''),
    available: res.available === true,
    reason: String(res.reason ?? ''),
    branch: String(res.branch ?? ''),
    detached: res.detached === true,
    dirty: Number(res.dirty ?? 0),
    dirty_capped: res.dirty_capped === true,
    commits: Array.isArray(res.commits) ? (res.commits as GitCommitEntry[]) : [],
    hasMore: res.has_more === true,
    branches: Array.isArray(res.branches) ? (res.branches as string[]) : []
  };
}

/**
 * A raw git response → the page, or a thrown refusal carrying the server's reason.
 *
 * The write actions answer the refreshed page on success and `{success: false,
 * reason}` (git's stderr for a 409) on a refusal; the reason is the only useful
 * thing to show the user, and the raw transport is what keeps it readable.
 * @param response The untouched response of the write action.
 * @param action Action name, used when the body carries no reason at all.
 */
async function readPage(response: globalThis.Response, action: string): Promise<GitGraphPage> {
  interface RefusalBody {
    success?: boolean;
    reason?: string;
    message?: string;
    [key: string]: unknown;
  }
  let body: RefusalBody | null = null;
  try {
    body = (await response.json()) as RefusalBody;
  } catch {
    // A body that is not JSON (an empty error page) leaves `body` null.
  }
  if (!response.ok || body?.success === false) {
    throw new Error(String(body?.reason || body?.message || `git ${action} failed (HTTP ${response.status})`));
  }
  return toPage(body ?? {});
}

/**
 * The declared payload of a read, refusing a null one.
 *
 * `fetchApiPayload` resolves to `null` for a failed request (the legacy bridge
 * contract) — for these callers that must become a rejection, because the panel
 * assigns the result to its state and a null page throws on the next render.
 * @param res Payload (null on failure).
 * @param what What was being read (names the error).
 */
function requirePayload<T>(res: T | null, what: string): T {
  if (!res) throw new Error(`git ${what} failed`);
  return res;
}

/**
 * Read one page of the session project's git history.
 * @param sessionId Session whose project directory is read.
 * @param options Paging (`limit` commits from `skip`).
 * @param options.limit
 * @param options.skip
 */
export async function fetchGitGraph(
  sessionId: string,
  options: { limit?: number; skip?: number } = {}
): Promise<GitGraphPage> {
  const res = await fetchApiPayload<Record<string, unknown> & { success?: boolean }>({
    url: '/git/graph',
    opts: {
      session_id: sessionId,
      limit: options.limit ?? 40,
      skip: options.skip ?? 0
    },
    method: 'get'
  });
  return toPage(requirePayload(res, 'graph'));
}

/**
 * Move the current branch to a commit (the panel's 回退 action).
 * @param sessionId Session whose project directory is written.
 * @param hash Target commit (any revision git can resolve to a commit).
 * @param mode `soft` keeps the changes staged, `mixed` unstages them, `hard` discards them.
 * @throws Error carrying git's own refusal (e.g. an unknown commit or a 409).
 */
export async function resetGitBranch(
  sessionId: string,
  hash: string,
  mode: 'soft' | 'mixed' | 'hard'
): Promise<GitGraphPage> {
  const resp = await fetchApiRaw({
    url: '/git/reset',
    method: 'post',
    body: JSON.stringify({ session_id: sessionId, hash, mode }),
    contentType: 'application/json'
  });
  return readPage(resp, 'reset');
}

/**
 * Check out a branch (or any ref git can resolve).
 * @param sessionId Session whose project directory is written.
 * @param ref Branch / tag / revision to check out.
 * @throws Error carrying git's own refusal — a dirty tree that a checkout would
 *   clobber comes back verbatim, so the toast can say which files block it.
 */
export async function checkoutGitRef(sessionId: string, ref: string): Promise<GitGraphPage> {
  const resp = await fetchApiRaw({
    url: '/git/checkout',
    method: 'post',
    body: JSON.stringify({ session_id: sessionId, ref }),
    contentType: 'application/json'
  });
  return readPage(resp, 'checkout');
}

/** One file a commit touched (the status is git's own letter). */
export interface GitCommitFile {
  /** `M` modified, `A` added, `D` deleted, `R` renamed, `C` copied, `T` type change. */
  status: string;
  path: string;
  /** The pre-rename path (only for `R` / `C`). */
  old_path: string;
}

/** One commit's metadata + the files it touched. */
export interface GitCommitDetail {
  hash: string;
  short: string;
  author: string;
  date: string;
  parents: string[];
  subject: string;
  files: GitCommitFile[];
}

/** One side of one aligned diff row (``null`` when the other column runs alone). */
export interface GitDiffLine {
  /** 1-based line number on that side. */
  n: number;
  text: string;
  kind: 'same' | 'add' | 'remove';
}

/** One rendered row of the side-by-side diff. */
export interface GitDiffRow {
  left: GitDiffLine | null;
  right: GitDiffLine | null;
}

/** One file's diff inside one commit, already aligned. */
export interface GitCommitDiff {
  hash: string;
  path: string;
  old_path: string;
  status: string;
  /** `<short-hash>:<path>` labels (empty on the side the file does not exist). */
  old_label: string;
  new_label: string;
  rows: GitDiffRow[];
  /** The row cap was hit (the tail of the diff is not shown). */
  truncated: boolean;
  /** One side is not text (nothing to align). */
  binary: boolean;
  /** `too-large` when a side exceeds the read bound. */
  notice: string;
}

/**
 * Read one commit's file list.
 * @param sessionId Session whose project directory is read.
 * @param hash Commit to inspect.
 */
export async function fetchCommitFiles(sessionId: string, hash: string): Promise<GitCommitDetail> {
  const res = await fetchApiPayload<Record<string, unknown> & { success?: boolean }>({
    url: '/git/commit',
    opts: { session_id: sessionId, hash },
    method: 'get'
  });
  requirePayload(res, 'commit');
  return {
    hash: String(res.hash ?? ''),
    short: String(res.short ?? ''),
    author: String(res.author ?? ''),
    date: String(res.date ?? ''),
    parents: Array.isArray(res.parents) ? (res.parents as string[]) : [],
    subject: String(res.subject ?? ''),
    files: Array.isArray(res.files) ? (res.files as GitCommitFile[]) : []
  };
}

/**
 * Read one file's aligned diff inside one commit.
 * @param sessionId Session whose project directory is read.
 * @param hash Commit to inspect.
 * @param path File path inside the commit (its new path for a rename).
 */
export async function fetchCommitDiff(sessionId: string, hash: string, path: string): Promise<GitCommitDiff> {
  const res = await fetchApiPayload<Record<string, unknown> & { success?: boolean }>({
    url: '/git/commit/file',
    opts: { session_id: sessionId, hash, path },
    method: 'get'
  });
  requirePayload(res, 'commit file');
  return {
    hash: String(res.hash ?? ''),
    path: String(res.path ?? path),
    old_path: String(res.old_path ?? ''),
    status: String(res.status ?? 'M'),
    old_label: String(res.old_label ?? ''),
    new_label: String(res.new_label ?? ''),
    rows: Array.isArray(res.rows) ? (res.rows as GitDiffRow[]) : [],
    truncated: res.truncated === true,
    binary: res.binary === true,
    notice: String(res.notice ?? '')
  };
}
