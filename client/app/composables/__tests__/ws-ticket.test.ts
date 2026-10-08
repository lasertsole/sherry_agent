/**
 * WebSocket handshake tickets: prefetch, spend-once, no-op without enforcement.
 *
 * A ticket is single-use, so the contract that matters is: one mint per connect,
 * appended to that connect's URL, and gone afterwards — otherwise a reconnect
 * would replay a spent ticket and the socket would be refused. Loopback clients
 * (authRequired !== true) must never mint anything.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { appendWsTicket, clearWsTicket, prepareWsTicket } from '@/composables/ws-ticket';
import { useAuthStore } from '@/stores/auth';

const bridge = vi.hoisted(() => ({ fetchWsTicket: vi.fn() }));
vi.mock('~/composables/bridge/auth', () => bridge);

/** Let the lazy dynamic import inside the mint settle (one macrotask). */
const flushMint = () => new Promise(resolve => setTimeout(resolve, 0));

describe('ws-ticket', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    // Drive the REAL store through the auto-imported symbol (see setup.ts).
    vi.stubGlobal('useAuthStore', () => useAuthStore());
    bridge.fetchWsTicket.mockReset();
    clearWsTicket();
  });

  it('appends a minted ticket and spends it', async () => {
    const auth = useAuthStore();
    auth.authRequired = true;
    bridge.fetchWsTicket.mockResolvedValue('ticket-1');

    await prepareWsTicket();
    await flushMint();
    expect(bridge.fetchWsTicket).toHaveBeenCalledTimes(1);

    expect(appendWsTicket('/sessions/ws?session_id=s1')).toBe('/sessions/ws?session_id=s1&ticket=ticket-1');
    // Spent: the next URL gets nothing until a new ticket is prepared.
    expect(appendWsTicket('/sessions/ws?session_id=s1')).toBe('/sessions/ws?session_id=s1');
  });

  it('uses ? when the url has no query yet', async () => {
    const auth = useAuthStore();
    auth.authRequired = true;
    bridge.fetchWsTicket.mockResolvedValue('t2');

    await prepareWsTicket();
    await flushMint();

    expect(appendWsTicket('/subagents/ws')).toBe('/subagents/ws?ticket=t2');
  });

  it('shares one mint between parallel prepares', async () => {
    const auth = useAuthStore();
    auth.authRequired = true;
    let resolveTicket: (value: string) => void = () => {};
    bridge.fetchWsTicket.mockImplementation(() => new Promise<string>(resolve => (resolveTicket = resolve)));

    const first = prepareWsTicket();
    const second = prepareWsTicket();
    // The mint is reached through a lazy dynamic import, so let it land first.
    await vi.waitFor(() => expect(bridge.fetchWsTicket).toHaveBeenCalledTimes(1));
    resolveTicket('t3');
    await Promise.all([first, second]);

    expect(bridge.fetchWsTicket).toHaveBeenCalledTimes(1);
  });

  it('mints nothing while no client needs one', async () => {
    const auth = useAuthStore();
    auth.authRequired = false;

    await prepareWsTicket();

    expect(bridge.fetchWsTicket).not.toHaveBeenCalled();
    expect(appendWsTicket('/sessions/ws')).toBe('/sessions/ws');
  });

  it('survives a failed mint and retries on the next prepare', async () => {
    const auth = useAuthStore();
    auth.authRequired = true;
    bridge.fetchWsTicket.mockRejectedValueOnce(new Error('offline'));

    await prepareWsTicket();
    await flushMint();
    expect(appendWsTicket('/sessions/ws')).toBe('/sessions/ws');

    bridge.fetchWsTicket.mockResolvedValueOnce('t4');
    await prepareWsTicket();
    await flushMint();
    expect(appendWsTicket('/sessions/ws')).toBe('/sessions/ws?ticket=t4');
  });
});
