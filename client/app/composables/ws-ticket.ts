/**
 * WebSocket handshake tickets.
 *
 * Every socket now passes a second gate while login protection is on: a
 * single-use ticket minted over HTTP (where the backend can see the caller's
 * address and apply the loopback exemption). Tickets are spent by the URL
 * builder, so a reconnect must prefetch a fresh one — hence the two-phase shape
 * below: `prepareWsTicket()` before the connect, `appendWsTicket()` while
 * building the URL.
 *
 * When nothing enforces a login (`authRequired !== true`, e.g. loopback) nothing
 * is minted and the URL is returned untouched — the desktop app's sockets stay
 * exactly as they were.
 */

/** The prefetched ticket, or null. Single use: it is cleared when spent. */
let cached: string | null = null;
/** An in-flight mint, so parallel connects share one request. */
let inFlight: Promise<void> | null = null;

/**
 * Make sure a ticket is ready for the next connect (no-op when none is needed).
 */
export async function prepareWsTicket(): Promise<void> {
  const auth = useAuthStore();
  if (auth.authRequired !== true || cached) return;
  if (!inFlight) {
    inFlight = (async () => {
      try {
        // Imported lazily: the socket layer must not pull the auth bridge (and
        // therefore the transport module) into every page's module graph, and
        // suites that partially mock the transport stay loadable.
        const { fetchWsTicket } = await import('./bridge/auth');
        cached = (await fetchWsTicket()) || null;
      } catch {
        // The socket will be refused; the next preparation retries after login.
        cached = null;
      } finally {
        inFlight = null;
      }
    })();
  }
  await inFlight;
}

/**
 * Append the prepared ticket to a WebSocket URL and spend it.
 * @param url
 */
export function appendWsTicket(url: string): string {
  const ticket = cached ?? '';
  cached = null;
  if (!ticket) return url;
  return `${url}${url.includes('?') ? '&' : '?'}ticket=${encodeURIComponent(ticket)}`;
}

/** Drop any prepared ticket (logout, protection disabled). Used by tests too. */
export function clearWsTicket(): void {
  cached = null;
}
