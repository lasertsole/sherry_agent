/**
 * The auth store: status flags, login/logout, account changes, error codes.
 *
 * The store owns no token — the session is an HttpOnly cookie — so what these
 * tests pin is the state machine: status flags drive the guard, a login starts
 * the refresh timer, failures land in `errorCode`/`errorMessage` for the form,
 * and logout drops the user even when the revoke call fails.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useAuthStore } from '@/stores/auth';
import { clearSessionRefresh, hasScheduledRefresh } from '@/composables/use-auth-refresh';

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

describe('useAuthStore', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    // Drive the REAL store through the auto-imported symbol (see setup.ts).
    vi.stubGlobal('useAuthStore', () => useAuthStore());
    Object.values(bridge).forEach(fn => fn.mockReset());
    // The scheduler is consumed by the store as a module-bound auto-import, so
    // it is observed through the REAL module's state instead of a stub.
    clearSessionRefresh();
    bridge.fetchAuthStatus.mockResolvedValue({
      authRequired: false,
      authEnabled: false,
      hasAccount: false
    });
  });

  it('applies the status flags and remembers that it checked', async () => {
    const store = useAuthStore();
    expect(store.authRequired).toBeNull();

    await store.checkAuthStatus();

    expect(store.authRequired).toBe(false);
    expect(store.checked).toBe(true);
  });

  it('caches the probe unless forced', async () => {
    const store = useAuthStore();
    await store.checkAuthStatus();
    await store.checkAuthStatus();
    expect(bridge.fetchAuthStatus).toHaveBeenCalledTimes(1);

    await store.checkAuthStatus(true);
    expect(bridge.fetchAuthStatus).toHaveBeenCalledTimes(2);
  });

  it('treats an unreachable backend as "no login required" instead of trapping the app', async () => {
    bridge.fetchAuthStatus.mockRejectedValue(new Error('offline'));
    const store = useAuthStore();

    await store.checkAuthStatus();

    expect(store.authRequired).toBe(false);
    expect(store.checked).toBe(true);
  });

  it('logs in, stores the user and schedules the rotation', async () => {
    bridge.loginRequest.mockResolvedValue({
      user: { id: 1, username: 'admin' },
      expiresIn: 43200
    });
    const store = useAuthStore();

    expect(await store.login('admin', 'hunter2-long')).toBe(true);

    expect(store.user).toEqual({ id: 1, username: 'admin' });
    expect(store.authRequired).toBe(false);
    expect(hasScheduledRefresh()).toBe(true);
  });

  it('keeps the failure code for the form and schedules nothing', async () => {
    const failure = Object.assign(new Error('invalid username or password'), {
      code: 'invalid_credentials'
    });
    bridge.loginRequest.mockRejectedValue(failure);
    const store = useAuthStore();

    expect(await store.login('admin', 'nope')).toBe(false);

    expect(store.errorCode).toBe('invalid_credentials');
    expect(store.errorMessage).toContain('invalid username or password');
    expect(store.user).toBeNull();
    expect(hasScheduledRefresh()).toBe(false);
  });

  it('drops the session on logout even when the revoke call fails', async () => {
    bridge.loginRequest.mockResolvedValue({ user: { id: 1, username: 'a' }, expiresIn: 60 });
    bridge.logoutRequest.mockRejectedValue(new Error('offline'));
    const store = useAuthStore();
    await store.login('a', 'hunter2-long');

    await store.logout();

    expect(store.user).toBeNull();
    expect(store.authRequired).toBe(true);
    expect(hasScheduledRefresh()).toBe(false);
  });

  it('sets up the first account and marks protection enabled', async () => {
    bridge.setupAccountRequest.mockResolvedValue({ id: 1, username: 'admin' });
    const store = useAuthStore();

    expect(await store.setupAccount('admin', 'hunter2-long')).toBe(true);

    expect(store.hasAccount).toBe(true);
    expect(store.authEnabled).toBe(true);
    expect(store.user?.username).toBe('admin');
  });

  it('surfaces the backend code when a password change is refused', async () => {
    bridge.updateAccountRequest.mockRejectedValue(
      Object.assign(new Error('the current password is incorrect'), { code: 'invalid_password' })
    );
    const store = useAuthStore();

    expect(await store.updateAccount('wrong', undefined, 'brand-new-pass')).toBe(false);
    expect(store.errorCode).toBe('invalid_password');
  });

  it('disables and re-enables protection', async () => {
    bridge.disableAuthRequest.mockResolvedValue(undefined as never);
    bridge.enableAuthRequest.mockResolvedValue(undefined as never);
    const store = useAuthStore();
    store.authEnabled = true;

    expect(await store.disableAuth('hunter2-long')).toBe(true);
    expect(store.authEnabled).toBe(false);
    expect(hasScheduledRefresh()).toBe(false);

    expect(await store.enableAuth('hunter2-long')).toBe(true);
    expect(store.authEnabled).toBe(true);
  });

  it('fetchUser reads the cookie session and clears it when gone', async () => {
    const store = useAuthStore();
    bridge.fetchMe.mockResolvedValueOnce({ id: 2, username: 'b' });
    expect((await store.fetchUser())?.username).toBe('b');

    bridge.fetchMe.mockResolvedValueOnce(null);
    expect(await store.fetchUser()).toBeNull();
    expect(store.user).toBeNull();
  });

  it('clearError empties the rendered failure', async () => {
    const store = useAuthStore();
    store.errorCode = 'x';
    store.errorMessage = 'y';

    store.clearError();

    expect(store.errorCode).toBeNull();
    expect(store.errorMessage).toBeNull();
  });
});
