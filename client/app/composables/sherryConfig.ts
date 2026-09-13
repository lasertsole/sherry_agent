export interface SherryEntry {
  key: string;
  value: string;
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
 * scope (see ConfigDialog.vue's setup-context watch).
 */
export async function readSherryConfig(): Promise<SherryConfigPayload> {
  const res = await fetchApi({
    url: '/sherry-config',
    opts: { _ts: Date.now() },
    method: 'get'
  });
  // `res` is never null in practice on success. Guard against an empty body
  // while preserving the `{ entries }` shape so a non-throw path never yields
  // an unreadable value.
  return (res as unknown as SherryConfigPayload | undefined) || { entries: [] };
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
