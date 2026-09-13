/** Backend HTTP base URL (REST): `VITE_API_BACK_URL`, falling back to the local backend address so requests never degrade into relative paths that hit the Nuxt dev server. */
export const API_BASE_URL: string = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';

/** Backend WebSocket base URL: `API_BASE_URL` with `http(s)://` rewritten to `ws(s)://` and trailing slashes stripped. */
export const WS_BASE_URL: string = resolveWsBaseUrl(API_BASE_URL);

/**
 * Resolve the WebSocket base URL from an HTTP API base URL.
 *
 * `http://host:port` -> `ws://host:port`, `https://` -> `wss://`,
 * and strips any trailing slashes.
 * @param apiBaseUrl
 */
export function resolveWsBaseUrl(apiBaseUrl: string): string {
  return apiBaseUrl.replace(/^https?:\/\//, m => (m === 'https://' ? 'wss://' : 'ws://')).replace(/\/+$/, '');
}

export interface EnvEntry {
  key: string;
  value: string;
  value_edited: boolean;
}

export interface EnvGroup {
  name: string;
  entries: EnvEntry[];
}

/** Raw structure returned by the backend `/env` endpoint (a list of groups) */
export interface EnvConfigPayload {
  groups: EnvGroup[];
}

/**
 * Read the project's .env configuration (grouped by prefix).
 * Throws on request failure so callers can distinguish a "request error" from a
 * genuinely empty .env, avoiding misreporting network/auth failures as "no .env file
 * found".
 */
export async function readEnvConfig(): Promise<EnvConfigPayload> {
  // Backend `GET /env` returns the group payload directly (NOT wrapped in
  // { code, data, msg }). So the resolved response IS `{ groups: [...] }`.
  //
  // Caller-context requirement: `fetchApi` is built on Nuxt's `useFetch` with
  // `server:true`. In a pure SPA, `useFetch` only issues a real request when
  // called from a live setup context (getCurrentInstance() != null). Calling it
  // from an event handler (e.g. PrimeVue TabPanel `@show`) yields `data ===
  // undefined` without any network round-trip. The caller MUST trigger this
  // from setup scope (see ConfigDialog.vue's setup-context watch), otherwise the
  // `|| { groups: [] }` fallback below misleads the user into thinking the
  // `.env` file is missing.
  const res = await fetchApi({
    url: '/env',
    opts: { _ts: Date.now() },
    method: 'get'
  });
  // `res` is never null in practice on success (backend responds with the full
  // payload). Guard against an empty body while preserving the `{ groups }`
  // shape so a non-throw path never yields an unreadable value.
  return (res as unknown as EnvConfigPayload | undefined) || { groups: [] };
}

/**
 * Update the values of keys that already exist in .env.
 * @param changes Shaped like { KEY: "new value" }; only keys already present in the
 *   file are accepted.
 * @returns true on success, false on failure
 */
export async function writeEnvConfig(changes: Record<string, string>): Promise<boolean> {
  try {
    await fetchApi({
      url: '/env',
      opts: { changes },
      method: 'put'
    });
    return true;
  } catch {
    return false;
  }
}
