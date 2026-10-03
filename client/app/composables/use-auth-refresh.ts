/**
 * Session refresh scheduling.
 *
 * The access token lives in an HttpOnly cookie, so the timer is driven by the
 * `expires_in` the login/refresh bodies report rather than by decoding a token
 * (which the browser deliberately cannot read). A failure clears the schedule
 * instead of retrying forever: the route guard takes the user to /login on the
 * next navigation, and the transport's own 401 replay starts a fresh attempt.
 *
 * The rotation itself (single-flight, shared with that 401 replay) lives in
 * `requestApi.refreshSessionOnce` and is consumed here as a bare auto-imported
 * symbol — the repo bans relative imports of sibling composables.
 */

/** Rotate this many seconds before the access token expires. */
export const REFRESH_LEEWAY_SECONDS = 300;

/** Timer handle for the scheduled rotation (null when nothing is scheduled). */
let timer: ReturnType<typeof setTimeout> | null = null;

/**
 * Schedule the next rotation for a session that expires in `expiresIn` seconds.
 * @param expiresIn Lifetime the backend reported for the fresh access token.
 */
export function scheduleSessionRefresh(expiresIn: number): void {
  clearSessionRefresh();
  const seconds = Math.max(30, Math.floor(expiresIn) - REFRESH_LEEWAY_SECONDS);
  timer = setTimeout(() => {
    timer = null;
    void refreshSessionOnce().then(ok => {
      if (!ok) clearSessionRefresh();
    });
  }, seconds * 1000);
}

/** Cancel any scheduled rotation (logout, protection disabled). */
export function clearSessionRefresh(): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

/** Whether a rotation is currently scheduled (used by tests). */
export function hasScheduledRefresh(): boolean {
  return timer !== null;
}
