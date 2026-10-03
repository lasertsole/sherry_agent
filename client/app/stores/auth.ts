import { defineStore } from 'pinia';
import type { AuthUser } from '~/composables/bridge/auth';
// The store needs a stable module specifier so tests can vi.mock the bridge;
// the unimport injection is compile-time and leaves bare symbols unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  disableAuthRequest,
  enableAuthRequest,
  fetchAuthStatus,
  fetchMe,
  loginRequest,
  logoutRequest,
  setupAccountRequest,
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

  function _applyStatus(status: { authRequired: boolean; authEnabled: boolean; hasAccount: boolean }): void {
    authRequired.value = status.authRequired;
    authEnabled.value = status.authEnabled;
    hasAccount.value = status.hasAccount;
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
      // A backend that cannot answer must not trap the app behind a login page:
      // treat it as "no login required" and let the transport report failures.
      authRequired.value = false;
      checked.value = true;
    }
  }

  /**
   * Sign in and start the refresh timer.
   * @param username
   * @param password
   */
  async function login(username: string, password: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      const result = await loginRequest(username, password);
      user.value = result.user;
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
      clearSessionRefresh();
      busy.value = false;
    }
  }

  /** Read the session's user (the guard uses this to confirm a cookie session). */
  async function fetchUser(): Promise<AuthUser | null> {
    user.value = await fetchMe();
    return user.value;
  }

  /**
   * First-time setup: create the account and switch protection on.
   * @param username
   * @param password
   */
  async function setupAccount(username: string, password: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      user.value = await setupAccountRequest(username, password);
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
   * Change the username and/or password of the current account.
   * @param password Current password (always required by the backend).
   * @param newUsername
   * @param newPassword
   */
  async function updateAccount(password: string, newUsername?: string, newPassword?: string): Promise<boolean> {
    busy.value = true;
    clearError();
    try {
      user.value = await updateAccountRequest({ password, newUsername, newPassword });
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
    checked,
    busy,
    errorCode,
    errorMessage,
    checkAuthStatus,
    clearError,
    disableAuth,
    enableAuth,
    fetchUser,
    login,
    logout,
    setupAccount,
    updateAccount
  };
});
