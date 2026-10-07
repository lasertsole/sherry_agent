/**
 * Git-graph bridge (the left sidebar's read-only history panel).
 *
 * One call: {@link fetchGitGraph} mirrors `GET /git/graph` — one page of the
 * session project's history plus the header facts (branch, dirty count). The
 * backend answers `available: false` with a reason (`not-a-repository` /
 * `git-unavailable`) instead of an error, so the panel can show its own empty
 * state without a toast.
 *
 * @module bridge/git
 */
// ``fetchApiPayload`` is auto-imported from ~/composables/requestApi (the bridge
// convention: no explicit import of an auto-imported symbol).

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
    hasMore: res.has_more === true
  };
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
  return toPage(res);
}

/**
 * Move the current branch to a commit (the panel's 回退 action).
 * @param sessionId Session whose project directory is written.
 * @param hash Target commit (any revision git can resolve to a commit).
 * @param mode `soft` keeps the changes staged, `mixed` unstages them, `hard` discards them.
 */
export async function resetGitBranch(
  sessionId: string,
  hash: string,
  mode: 'soft' | 'mixed' | 'hard'
): Promise<GitGraphPage> {
  const res = await fetchApiPayload<Record<string, unknown> & { success?: boolean }>({
    url: '/git/reset',
    opts: { session_id: sessionId, hash, mode },
    method: 'post'
  });
  return toPage(res);
}

/**
 * Check out a branch (or any ref git can resolve).
 * @param sessionId Session whose project directory is written.
 * @param ref Branch / tag / revision to check out.
 */
export async function checkoutGitRef(sessionId: string, ref: string): Promise<GitGraphPage> {
  const res = await fetchApiPayload<Record<string, unknown> & { success?: boolean }>({
    url: '/git/checkout',
    opts: { session_id: sessionId, ref },
    method: 'post'
  });
  return toPage(res);
}
