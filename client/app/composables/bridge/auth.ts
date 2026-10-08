/**
 * Login bridge calls (`/auth/*`).
 *
 * These go through `fetchApiRaw` rather than `fetchApi`: the login flow owns its
 * own failure contract (a wrong password is an expected outcome with a code the
 * form renders, not a generic error toast) and it needs the raw response to read
 * the backend's `{success:false, error, message}` body.
 *
 * The browser stores the session cookies itself — every call must carry
 * `credentials: 'include'` (set by the transport) and no token ever appears in
 * a body.
 *
 * @module bridge/auth
 */

import { fetchApiRaw } from '../requestApi';

/** The signed-in account as the backend reports it. */
export interface AuthUser {
  id: number;
  username: string;
  created_at?: number;
}

/** `GET /auth/status`. */
export interface AuthStatus {
  /** This client must log in (enforcement is on and it is not loopback). */
  authRequired: boolean;
  /** Login protection is switched on. */
  authEnabled: boolean;
  /** An account exists. */
  hasAccount: boolean;
}

/** A refusal from an auth endpoint, carrying the backend's code. */
export class AuthRequestError extends Error {
  /** Backend error code (`invalid_credentials`, `password_policy`, …). */
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'AuthRequestError';
    this.code = code;
  }
}

/**
 * Parse a JSON body, tolerating an empty or non-JSON response.
 * @param response
 */
async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * POST/PUT/DELETE a JSON body to an auth endpoint and unwrap the payload.
 * @param url
 * @param method
 * @param body
 */
async function authRequest(
  url: string,
  method: 'post' | 'put' | 'delete' | 'get',
  body?: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const response = await fetchApiRaw({
    url,
    method,
    ...(body === undefined ? {} : { contentType: 'application/json', body: JSON.stringify(body) })
  });
  const payload = await readJson(response);
  if (!response.ok || payload.success === false) {
    const code = typeof payload.error === 'string' ? payload.error : `http_${response.status}`;
    const message = typeof payload.message === 'string' ? payload.message : `request failed (${response.status})`;
    throw new AuthRequestError(code, message);
  }
  return payload;
}

/** Whether this client must log in, plus the switch/account flags. */
export async function fetchAuthStatus(): Promise<AuthStatus> {
  const payload = await authRequest('/auth/status', 'get');
  return {
    authRequired: payload.auth_required === true,
    authEnabled: payload.auth_enabled === true,
    hasAccount: payload.has_account === true
  };
}

/**
 * Sign in; the cookies ride the response, so only the user comes back.
 * @param username
 * @param password
 */
export async function loginRequest(username: string, password: string): Promise<{ user: AuthUser; expiresIn: number }> {
  const payload = await authRequest('/auth/login', 'post', { username, password });
  return { user: payload.user as AuthUser, expiresIn: Number(payload.expires_in ?? 0) };
}

/** Rotate the session (refresh cookie only; no body). */
export async function refreshRequest(): Promise<{ user: AuthUser; expiresIn: number }> {
  const payload = await authRequest('/auth/refresh', 'post');
  return { user: payload.user as AuthUser, expiresIn: Number(payload.expires_in ?? 0) };
}

/** Revoke the session and clear the cookies. */
export async function logoutRequest(): Promise<void> {
  await authRequest('/auth/logout', 'post');
}

/** The signed-in user, or `null` when the session is gone. */
export async function fetchMe(): Promise<AuthUser | null> {
  try {
    const payload = await authRequest('/auth/me', 'get');
    return (payload.user as AuthUser) ?? null;
  } catch {
    return null;
  }
}

/**
 * First-time setup (or a full re-setup when no account exists yet).
 * @param username
 * @param password
 */
export async function setupAccountRequest(username: string, password: string): Promise<AuthUser> {
  const payload = await authRequest('/auth/account', 'put', { username, password });
  return payload.user as AuthUser;
}

/**
 * Change the current account (both halves require the current password).
 * @param options
 * @param options.password
 * @param options.newUsername
 * @param options.newPassword
 */
export async function updateAccountRequest(options: {
  password: string;
  newUsername?: string;
  newPassword?: string;
}): Promise<AuthUser> {
  const body: Record<string, unknown> = { password: options.password };
  if (options.newUsername !== undefined) body.new_username = options.newUsername;
  if (options.newPassword !== undefined) body.new_password = options.newPassword;
  const payload = await authRequest('/auth/account', 'put', body);
  return payload.user as AuthUser;
}

/**
 * Turn login protection back on for an existing account (password required).
 * @param password
 */
export async function enableAuthRequest(password: string): Promise<void> {
  await authRequest('/auth/account', 'put', { password, enabled: true });
}

/**
 * Turn login protection off (requires the current password).
 * @param password
 */
export async function disableAuthRequest(password: string): Promise<void> {
  await authRequest('/auth/account', 'delete', { password });
}

/** Mint a single-use WebSocket handshake ticket. */
export async function fetchWsTicket(): Promise<string> {
  const payload = await authRequest('/auth/ws-ticket', 'get');
  return String(payload.ticket ?? '');
}
