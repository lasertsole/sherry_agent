import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useWebSearchStore } from '../web-search';

const bridge = vi.hoisted(() => ({
  fetchSearchBackend: vi.fn(),
  setSearchBackend: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

// The store subscribes to the socket bus through the Nuxt auto-imported `on`
// (resolved to `@/composables/mitt`); mock it so the frame can be replayed by
// hand and no listener leaks between tests.
const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));
vi.mock('@/composables/mitt', () => mittMocks);

/** The handler the store registered for the downgrade frame, once subscribed. */
function fallbackHandler(): (payload: unknown) => void {
  return mittMocks.on.mock.calls.find(call => call[0] === 'ws:search_backend_fallback')?.[1] as (
    payload: unknown
  ) => void;
}

/** The wire shape `GET /sessions/search_backend` answers with. */
const state = {
  backend: null as 'tavily' | 'searxng' | null,
  effective: 'tavily' as 'tavily' | 'searxng',
  pending: false,
  pending_value: null as 'tavily' | 'searxng' | null,
  tavily_available: true,
  searxng: {
    configured: true,
    url: 'http://localhost:8888',
    engines: '',
    categories: '',
    proxy: '',
    verify_tls: true
  }
};

describe('stores/web-search', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchSearchBackend.mockReset();
    bridge.setSearchBackend.mockReset();
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
  });

  it('stateFor() is null before the first read', () => {
    const store = useWebSearchStore();
    expect(store.stateFor('sid-1')).toBeNull();
  });

  it('hydrate() mirrors the backend state', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValueOnce(state);

    await store.hydrate('sid-1');

    expect(bridge.fetchSearchBackend).toHaveBeenCalledWith('sid-1');
    expect(store.stateFor('sid-1')).toEqual(state);
  });

  it('hydrate() keeps the last state when the read fails', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValueOnce(state);
    await store.hydrate('sid-1');
    bridge.fetchSearchBackend.mockRejectedValueOnce(new Error('offline'));

    await store.hydrate('sid-1');

    expect(store.stateFor('sid-1')).toEqual(state);
  });

  it('select() stores the choice and the parked flag the backend reported', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValueOnce(state);
    await store.hydrate('sid-1');
    bridge.setSearchBackend.mockResolvedValueOnce({ backend: 'searxng', pending: true });

    await store.select('sid-1', 'searxng');

    expect(bridge.setSearchBackend).toHaveBeenCalledWith('sid-1', 'searxng');
    expect(store.stateFor('sid-1')?.backend).toBe('searxng');
    expect(store.stateFor('sid-1')?.pending).toBe(true);
  });

  it('select() rolls the session back to its previous state on failure', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValueOnce(state);
    await store.hydrate('sid-1');
    bridge.setSearchBackend.mockRejectedValueOnce(new Error('refused'));

    await store.select('sid-1', 'searxng');

    expect(store.stateFor('sid-1')).toEqual(state);
  });

  it('noteFallback() records the notice for the frame session and dismiss clears it', () => {
    const store = useWebSearchStore();

    store.noteFallback({
      session_id: 'sid-1',
      content: { backend: 'searxng', reason: 'tavily_auth_failed', message: 'Tavily 无法使用' }
    });

    expect(store.fallbackFor('sid-1')).toMatchObject({
      backend: 'searxng',
      reason: 'tavily_auth_failed'
    });
    expect(store.fallbackFor('sid-2')).toBeNull();

    store.dismissFallback('sid-1');
    expect(store.fallbackFor('sid-1')).toBeNull();
  });

  it('noteFallback() ignores a frame without a session id', () => {
    const store = useWebSearchStore();

    store.noteFallback({ content: { backend: 'searxng', reason: 'tavily_timeout' } });

    expect(store.fallbackBySession).toEqual({});
  });

  it('noteFallback() replaces an older undismissed notice', () => {
    const store = useWebSearchStore();

    store.noteFallback({ session_id: 'sid-1', content: { backend: 'searxng', reason: 'tavily_timeout' } });
    store.noteFallback({
      session_id: 'sid-1',
      content: { backend: 'searxng', reason: 'tavily_auth_failed' }
    });

    expect(store.fallbackFor('sid-1')?.reason).toBe('tavily_auth_failed');
  });

  it('init() hydrates once and is idempotent', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValue(state);

    store.init('sid-1');
    store.init('sid-1');
    await vi.waitFor(() => expect(store.stateFor('sid-1')).toEqual(state));

    expect(bridge.fetchSearchBackend).toHaveBeenCalledTimes(1);
  });

  it('subscribe() routes the socket frame into a notice exactly once', () => {
    const store = useWebSearchStore();

    store.subscribe();
    store.subscribe();

    expect(mittMocks.on.mock.calls.filter(call => call[0] === 'ws:search_backend_fallback')).toHaveLength(1);
    fallbackHandler()({
      event: 'search_backend_fallback',
      session_id: 'sid-9',
      content: { backend: 'searxng', reason: 'tavily_timeout', message: 'fallback' }
    });
    expect(store.fallbackFor('sid-9')?.reason).toBe('tavily_timeout');
  });
});
