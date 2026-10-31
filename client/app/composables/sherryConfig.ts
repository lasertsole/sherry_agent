export interface SherryEntry {
  key: string;
  value: string;
  /** The registry default (the App Config panel's 重置为默认值 restores this). */
  default?: string;
  value_edited: boolean;
}

/** Raw structure returned by the backend `/sherry-config` endpoint (a flat entry list) */
export interface SherryConfigPayload {
  entries: SherryEntry[];
}

/**
 * Read the project's sherry.jsonc application settings (flat entry list).
 * Throws on request failure so callers can distinguish a "request error" from
 * an empty config.
 *
 * Caller-context requirement: identical to `readEnvConfig` — `fetchApi` is
 * built on Nuxt's `useFetch` with `server:true`, which only issues a real
 * request from a live setup context. The caller MUST trigger this from setup
 * scope (see ConfigPanel.vue's setup-context watch).
 */
export async function readSherryConfig(): Promise<SherryConfigPayload> {
  const res = await fetchApi<SherryConfigPayload>({
    url: '/sherry-config',
    opts: { _ts: Date.now() },
    method: 'get'
  });
  // `res` is never null in practice on success. Guard against an empty body
  // while preserving the `{ entries }` shape so a non-throw path never yields
  // an unreadable value.
  return res || { entries: [] };
}

/**
 * Update values of keys that already exist in sherry.jsonc (values are coerced
 * to their declared types by the backend).
 * @param changes Shaped like { KEY: "new value" }; only known keys are accepted.
 * @returns true on success, false on failure
 */
export async function writeSherryConfig(changes: Record<string, string>): Promise<boolean> {
  try {
    await fetchApi({
      url: '/sherry-config',
      opts: { changes },
      method: 'put'
    });
    return true;
  } catch {
    return false;
  }
}

/** The list settings the backend accepts (its own allowlist is the authority). */
export type SherryListKey = 'regression_gate_allowed_executables' | 'yolo_deny_paths';

/** One list-setting payload (`GET /sherry-config/list/<key>`). */
export interface SherryListPayload {
  key: string;
  values: string[];
}

/**
 * Read one list setting (the USER's entries; the code owns the defaults).
 * Throws on request failure so a caller can tell an error from an empty list.
 * @param key
 */
export async function readSherryList(key: SherryListKey): Promise<string[]> {
  const res = await fetchApi<SherryListPayload>({
    url: `/sherry-config/list/${key}`,
    opts: { _ts: Date.now() },
    method: 'get'
  });
  return Array.isArray(res?.values) ? res.values : [];
}

/**
 * Replace one list setting; the backend normalizes (lowercase, dedupe) and
 * validates the entries, and answers the written list.
 * @param key
 * @param values
 * @returns The written list.
 */
export async function writeSherryList(key: SherryListKey, values: string[]): Promise<string[]> {
  const res = await fetchApi<SherryListPayload>({
    url: `/sherry-config/list/${key}`,
    opts: { values },
    method: 'put'
  });
  return Array.isArray(res?.values) ? res.values : [];
}
