import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { fallbackNoticeText, useWebSearchStore } from '../web-search';

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

// The downgrade notice is localized through the runtime resolver (a non-setup
// context cannot call `useI18n`).
const i18nRuntime = vi.hoisted(() => ({ resolveRuntimeT: vi.fn() }));
vi.mock('@/composables/i18nRuntime', () => i18nRuntime);

/** The notification store the store pushes into (Nuxt auto-import). */
const notifications = vi.hoisted(() => ({ handleNotification: vi.fn() }));

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
  },
  env: {
    searxng_url: 'http://localhost:8888',
    searxng_proxy: '',
    searxng_engines: '',
    searxng_categories: '',
    searxng_verify_tls: true,
    tavily_key_present: true
  }
};

/** The handler the store registered for the downgrade frame, once subscribed. */
function fallbackHandler(): (payload: unknown) => void {
  return mittMocks.on.mock.calls.find(call => call[0] === 'ws:search_backend_fallback')?.[1] as (
    payload: unknown
  ) => void;
}

describe('stores/web-search', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchSearchBackend.mockReset();
    bridge.setSearchBackend.mockReset();
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
    i18nRuntime.resolveRuntimeT.mockReset();
    notifications.handleNotification.mockReset();
    vi.stubGlobal(
      'useNotificationStore',
      vi.fn(() => notifications)
    );
  });

  it('stateFor() is null before the first read', () => {
    const store = useWebSearchStore();
    expect(store.stateFor('sid-1')).toBeNull();
  });

  it('hydrate() mirrors the backend state, env view included', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValueOnce(state);

    await store.hydrate('sid-1');

    expect(bridge.fetchSearchBackend).toHaveBeenCalledWith('sid-1');
    expect(store.stateFor('sid-1')).toEqual(state);
    expect(store.stateFor('sid-1')?.env.tavily_key_present).toBe(true);
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

  it('init() hydrates once and is idempotent', async () => {
    const store = useWebSearchStore();
    bridge.fetchSearchBackend.mockResolvedValue(state);

    store.init('sid-1');
    store.init('sid-1');
    await vi.waitFor(() => expect(store.stateFor('sid-1')).toEqual(state));

    expect(bridge.fetchSearchBackend).toHaveBeenCalledTimes(1);
  });

  it('subscribe() routes the socket frame into a 通知 entry exactly once', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue((key: string, params?: Record<string, unknown>) =>
      key === 'searchFallback.notice'
        ? `Tavily 无法使用（${params?.reason}），已切换到 ${params?.name}`
        : key === 'searchFallback.reason.tavily_timeout'
          ? '请求超时'
          : key
    );
    const store = useWebSearchStore();

    store.subscribe();
    store.subscribe();

    expect(mittMocks.on.mock.calls.filter(call => call[0] === 'ws:search_backend_fallback')).toHaveLength(1);

    fallbackHandler()({
      event: 'search_backend_fallback',
      session_id: 'sid-9',
      content: { backend: 'searxng', reason: 'tavily_timeout', message: 'fallback' }
    });

    expect(notifications.handleNotification).toHaveBeenCalledWith(
      'Tavily 无法使用（请求超时），已切换到 SearXNG',
      'web_search'
    );
  });

  it('handleFallbackFrame() ignores a frame without a session id', () => {
    const store = useWebSearchStore();

    store.handleFallbackFrame({ content: { backend: 'searxng', reason: 'tavily_timeout' } });

    expect(notifications.handleNotification).not.toHaveBeenCalled();
  });

  it('fallbackNoticeText() falls back to the frame message without i18n', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue(undefined);

    expect(fallbackNoticeText('searxng', 'tavily_error', 'Tavily 无法使用')).toBe('Tavily 无法使用');
    expect(fallbackNoticeText('searxng', 'tavily_error', '')).toBe('searxng (tavily_error)');
  });

  it('fallbackNoticeText() keeps an unknown reason verbatim', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue((key: string, params?: Record<string, unknown>) =>
      key === 'searchFallback.notice' ? `Tavily 无法使用（${params?.reason}），已切换到 ${params?.name}` : key
    );

    expect(fallbackNoticeText('searxng', 'tavily_quota', 'raw')).toBe(
      'Tavily 无法使用（tavily_quota），已切换到 SearXNG'
    );
  });

  it('fallbackNoticeText() renders the breaker notices with their own copy', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue((key: string, params?: Record<string, unknown>) =>
      key === 'searchFallback.cooldown'
        ? `SearXNG 连续失败，已暂停使用（约 ${params?.seconds} 秒后自动恢复）`
        : key === 'searchFallback.recovered'
          ? 'SearXNG 冷却结束，已恢复使用'
          : key
    );

    expect(fallbackNoticeText('searxng', 'searxng_cooldown_opened', 'raw', 60)).toBe(
      'SearXNG 连续失败，已暂停使用（约 60 秒后自动恢复）'
    );
    expect(fallbackNoticeText('searxng', 'searxng_cooldown_recovered', 'raw')).toBe('SearXNG 冷却结束，已恢复使用');
    // The generic "Tavily 无法使用…已切换到 X" template must never wrap a
    // breaker reason — without the dedicated keys the frame message wins.
    i18nRuntime.resolveRuntimeT.mockReturnValue((key: string) => key);
    expect(fallbackNoticeText('searxng', 'searxng_cooldown_opened', '原始消息', 60)).toBe('原始消息');
  });

  it('handleFallbackFrame() forwards the breaker frame with its window length', () => {
    i18nRuntime.resolveRuntimeT.mockReturnValue((key: string, params?: Record<string, unknown>) =>
      key === 'searchFallback.cooldown' ? `暂停 ${params?.seconds} 秒` : key
    );
    const store = useWebSearchStore();
    store.subscribe();

    fallbackHandler()({
      event: 'search_backend_fallback',
      session_id: 'sid-9',
      content: {
        backend: 'searxng',
        reason: 'searxng_cooldown_opened',
        message: 'raw',
        cooldown_seconds: 60
      }
    });

    expect(notifications.handleNotification).toHaveBeenCalledWith('暂停 60 秒', 'web_search');
  });
});
