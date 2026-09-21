/**
 * URL-pathname session-id resolution.
 *
 * Two consumers parse the active session id from the URL independently before
 * this module existed:
 * - `composables/subagent-sync.ts::resolveSid()` — trailing segment with `home`
 *   reserved, `undefined` when the path carries no session;
 * - `stores/todo.ts::resolveSid()` — the same parse plus `tasks` reserved (the
 *   standalone background-tasks route shares the `/home/tasks/<sid>` prefix),
 *   normalizing "no session" to `''`.
 *
 * The parse itself is identical; only the reserved-segment set and the
 * empty-result mapping differ, so both now delegate here.
 */

/** Route segments that are never a session id (the shell route). */
export const RESERVED_SESSION_SEGMENTS = ['home'] as const;

/**
 * Resolve the session id from a URL pathname: its last non-empty segment,
 * unless that segment is one of the reserved route segments.
 * @param pathname URL pathname (undefined = no location context, e.g. module scope on the server)
 * @param reserved Segments that are routes rather than session ids (defaults to the shell route)
 * @returns The bare session id, or undefined when the pathname carries none
 */
export function sessionIdFromPathname(
  pathname: string | undefined,
  reserved: readonly string[] = RESERVED_SESSION_SEGMENTS
): string | undefined {
  if (!pathname) return undefined;
  const segs = pathname.split('/').filter(Boolean);
  const last = segs[segs.length - 1];
  return last && !reserved.includes(last) ? last : undefined;
}
