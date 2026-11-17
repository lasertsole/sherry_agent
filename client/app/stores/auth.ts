import { defineStore } from 'pinia';
import type { AuthUser, TenantInfo } from '~/composables/bridge/auth';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  disableAuthRequest,
  enableAuthRequest,
  fetchAuthStatus,
  fetchMe,
  fetchTenants,
  loginRequest,
  logoutRequest,
  setupAccountRequest,
  switchTenantRequest,
  updateAccountRequest
} from '~/composables/bridge/auth';
// `scheduleSessionRefresh` / `clearSessionRefresh` are consumed as bare
// auto-imported symbols (the repo's no-restricted-imports rule forbids importing
// app/composables exports; tests stub them globally the same way).

/**
 * Login state for the whole app (pages, the route guard and the account panel).
 *
 * The backend is the only authority: `checkAuthStatus` asks `/auth/status` once
 * per app load (/login included) and everything else reads those three flags.
 * No token ever lands in this store — the session lives in HttpOnly cookies, so
 * there is nothing here for a script to steal.
 */
export const useAuthStore = defineStore('auth', () => {
  /** The signed-in user, or null. */
  const user = ref<AuthUser | null>(null);
  /** Whether THIS client must log in (null until the first status probe). */
  const authRequired = ref<boolean | null>(null);
  /** Login protection is switched on (any client). */
  const authEnabled = ref(false);
  /** An account exists on the backend. */
  const hasAccount = ref(false);
  /** Accounts are identified by username too (multi-tenant Ingress). */
  const usernameRequired = ref(false);
  /** Every cell this account may use (empty for a standalone cell). */
  const tenants = ref<TenantInfo[]>([]);
  /** The cell this browser currently uses; the Ingress cookie is authoritative. */
  const activeTenant = ref('');
  /** A status probe has completed at least once. */
  const checked = ref(false);
  /** A login/account request is in flight (forms disable their buttons). */
  const busy = ref(false);
  /** Last failure code + message, for the forms to render in place. */
  const errorCode = ref<string | null>(null);
  const errorMessage = ref<string | null>(null);

  function _setError(code: string | null, message: string | null): void {
    errorCode.value = code;
    errorMessage.value = message;
  }

  /** Clear the last failure (called when a form opens or a field changes). */
  function clearError(): void {
    _setError(null, null);
  }

  function _applyStatus(status: {
    authRequired: boolean;
    authEnabled: boolean;
    hasAccount: boolean;
    usernameRequired?: boolean;
  }): void {
    authRequired.value = status.authRequired;
    authEnabled.value = status.authEnabled;
    hasAccount.value = status.hasAccount;
    usernameRequired.value = status.usernameRequired === true;
    checked.value = true;
  }

  /**
   * Ask the backend whether this client must log in.
   * @param force Re-probe instead of using the cached flags.
   */
  async function checkAuthStatus(force = false): Promise<void> {
    if (checked.value && !force) return;
    try {
      _applyStatus(await fetchAuthStatus());
    } catch {
      // A probe that could not be answered decides NOTHING: the previous verdict
      // (or "unknown") stands and the next navigation re-probes. Treating the
      // failure as "no login required" used to send an expired session's user
      // away from /login — the guard bounced it home because the probe failed,
      // and the app then rendered 401s with no way back to the form.
      checked.value = false;
    }
  }

  /**
   * The backend refused the session (rotation or replay answered 401): drop the
   * stale user so the guard shows /login, and demand a login from now on.
   */
  function markSessionLost(): void {
    user.value = null;
    authRequired.value = true;
    tenants.value = [];
    activeTenant.value = '';
    clearSessionRefresh();
  }

  /**
   * Sign in with the password and start the refresh timer.
   * @param password
   * @param username Account name when the backend requires one (Ingress).
   */
  async function login(password: string, username?: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      const result = username === undefined ? await loginRequest(password) : await loginRequest(password, username);
      user.value = result.user;
      activeTenant.value = String(result.user.tenant_id ?? '');
      authRequired.value = false;
      authEnabled.value = true;
      hasAccount.value = true;
      scheduleSessionRefresh(result.expiresIn);
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code ?? 'login_failed';
      const message = e instanceof Error ? e.message : String(e);
      _setError(code, message);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** Revoke the session and forget the user. */
  async function logout(): Promise<void> {
    busy.value = true;
    try {
      await logoutRequest();
    } catch {
      // The session is being dropped regardless: a failed revoke must not keep
      // the UI thinking it is signed in.
    } finally {
      user.value = null;
      authRequired.value = true;
      tenants.value = [];
      activeTenant.value = '';
      clearSessionRefresh();
      busy.value = false;
    }
  }

  /** Read the session's user (the guard uses this to confirm a cookie session). */
  async function fetchUser(): Promise<AuthUser | null> {
    user.value = await fetchMe();
    return user.value;
  }

  /** Load the member-cell list (the Ingress supplies it; a cell answers 404). */
  async function loadTenants(): Promise<void> {
    try {
      const payload = await fetchTenants();
      tenants.value = payload.tenants;
      activeTenant.value = payload.active;
    } catch {
      // A standalone cell has no tenant switcher; leave it hidden.
      tenants.value = [];
      activeTenant.value = '';
    }
  }

  /**
   * Switch the browser to another member cell and reload the SPA.
   *
   * Every cached session, memory entry and WebSocket belongs to the previous
   * cell, so a full reload is the supported reset. `reload` is false in unit
   * tests only.
   * @param tenantId Cell to select.
   * @param reload Set false only in unit tests; production reloads the SPA.
   */
  async function switchTenant(tenantId: string, reload = true): Promise<boolean> {
    if (!tenantId || tenantId === activeTenant.value) return true;
    busy.value = true;
    clearError();
    try {
      await switchTenantRequest(tenantId);
      activeTenant.value = tenantId;
      if (reload && typeof window !== 'undefined') window.location.reload();
      return true;
    } catch (e) {
      _setError('tenant_switch_failed', e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * First-time setup: set the password and switch protection on.
   * @param password
   */
  async function setupAccount(password: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      user.value = await setupAccountRequest(password);
      authEnabled.value = true;
      hasAccount.value = true;
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code ?? 'setup_failed';
      _setError(code, e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * Replace the password (the current one is always required by the backend).
   * @param password Current password.
   * @param newPassword
   */
  async function updateAccount(password: string, newPassword: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      user.value = await updateAccountRequest({ password, newPassword });
      hasAccount.value = true;
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code ?? 'update_failed';
      _setError(code, e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * Turn login protection back on for an existing account.
   * @param password Current password.
   */
  async function enableAuth(password: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      await enableAuthRequest(password);
      authEnabled.value = true;
      authRequired.value = true;
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code ?? 'enable_failed';
      _setError(code, e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * Turn login protection off (the account stays for a later re-enable).
   * @param password Current password.
   */
  async function disableAuth(password: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      await disableAuthRequest(password);
      authEnabled.value = false;
      authRequired.value = false;
      clearSessionRefresh();
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code ?? 'disable_failed';
      _setError(code, e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      busy.value = false;
    }
  }

  return {
    user,
    authRequired,
    authEnabled,
    hasAccount,
    usernameRequired,
    tenants,
    activeTenant,
    checked,
    busy,
    errorCode,
    errorMessage,
    checkAuthStatus,
    clearError,
    disableAuth,
    enableAuth,
    fetchUser,
    loadTenants,
    login,
    logout,
    markSessionLost,
    setupAccount,
    switchTenant,
    updateAccount
  };
});
