/**
 * The global auth guard: who gets bounced to /login, and who never sees it.
 *
 * The middleware reads the backend's `auth_required` flag, so the cases that
 * matter are: nothing enforced → /login bounces home (a stale bookmark cannot
 * strand a user on a pointless form); enforced + no session → /login; enforced +
 * cookie session → through; /login with a live session → home.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useAuthStore } from '@/stores/auth';

const bridge = vi.hoisted(() => ({
  fetchAuthStatus: vi.fn(),
  loginRequest: vi.fn(),
  logoutRequest: vi.fn(),
  fetchMe: vi.fn(),
  setupAccountRequest: vi.fn(),
  updateAccountRequest: vi.fn(),
  disableAuthRequest: vi.fn(),
  enableAuthRequest: vi.fn(),
  fetchWsTicket: vi.fn()
}));
vi.mock('~/composables/bridge/auth', () => bridge);

const navigate = vi.hoisted(() => vi.fn((to: string) => `navigated:${to}`));
const addMiddleware = vi.hoisted(() => vi.fn());

/** Load the guard with the Nuxt globals it relies on stubbed. */
async function loadMiddleware() {
  vi.stubGlobal('navigateTo', navigate);
  vi.stubGlobal('useAuthStore', () => useAuthStore());
  // Nuxt compile-time macros: the plugin's default export uses both.
  vi.stubGlobal('defineNuxtPlugin', (fn: unknown) => fn);
  vi.stubGlobal('addRouteMiddleware', addMiddleware);
  const module = await import('@/plugins/auth-guard');
  const guard = module.authGuard as unknown;
  return guard as (to: { path: string; query?: Record<string, string> }) => Promise<unknown>;
}

describe('auth guard plugin', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    Object.values(bridge).forEach(fn => fn.mockReset());
    navigate.mockClear();
    addMiddleware.mockClear();
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: false,
      hasAccount: false
    });
    bridge.fetchMe.mockResolvedValue(null);
  });

  it('bounces /login home when nothing enforces a login', async () => {
    const guard = await loadMiddleware();

    expect(await guard({ path: '/login' })).toBe('navigated:/home');
  });

  it('passes other pages through when nothing is enforced', async () => {
    const guard = await loadMiddleware();

    expect(await guard({ path: '/home' })).toBeUndefined();
  });

  it('sends an unauthenticated client to /login when enforcement is on', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: true,
      authEnabled: true,
      hasAccount: true
    });
    const guard = await loadMiddleware();

    expect(await guard({ path: '/home' })).toBe('navigated:/login');
  });

  it('lets a cookie session through', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: true,
      authEnabled: true,
      hasAccount: true
    });
    bridge.fetchMe.mockResolvedValue({ id: 1, username: 'admin' });
    const guard = await loadMiddleware();

    expect(await guard({ path: '/home' })).toBeUndefined();
  });

  it('sends a signed-in client away from /login', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: true,
      authEnabled: true,
      hasAccount: true
    });
    bridge.fetchMe.mockResolvedValue({ id: 1, username: 'admin' });
    const guard = await loadMiddleware();

    expect(await guard({ path: '/login' })).toBe('navigated:/home');
  });

  it('keeps an enforced /login reachable while no session exists', async () => {
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: true,
      authEnabled: true,
      hasAccount: true
    });
    const guard = await loadMiddleware();

    expect(await guard({ path: '/login' })).toBeUndefined();
  });

  it('registers itself as a GLOBAL route middleware', async () => {
    const module = await import('@/plugins/auth-guard');

    (module.default as unknown as () => void)();

    expect(addMiddleware).toHaveBeenCalledWith('auth-guard', module.authGuard, { global: true });
  });
});
