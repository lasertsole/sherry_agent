/**
 * Lightweight model-config fetcher with module-level cache.
 *
 * Used by:
 *  - use-chat-stream.ts (toast on send when config is invalid)
 *  - ConfigDialog.vue (invalidate cache after env save)
 */

export interface ModelConfig {
  main_max_token: number | null;
  aux_max_token: number | null;
  min_required: number;
  valid: boolean;
}

/** Default-invalid config used as the fallback for failed/empty responses. */
function invalidModelConfig(): ModelConfig {
  return {
    main_max_token: null,
    aux_max_token: null,
    min_required: 131072,
    valid: false
  };
}

let cached: ModelConfig | null = null;

/**
 * Fetch the current model config from GET /model-config.
 * Resolves a default-invalid object on an empty body; throws on request failure
 * so callers can distinguish network errors.
 * @returns The current model config payload.
 */
export async function fetchModelConfig(): Promise<ModelConfig> {
  const res = await fetchApi({
    url: '/model-config',
    opts: { _ts: Date.now() },
    method: 'get'
  });
  return (res as unknown as ModelConfig | undefined) ?? invalidModelConfig();
}

/**
 * Return the cached config, fetching once if needed.
 * Silently returns a default-invalid object on fetch failure
 * (so handleSend's toast guard never breaks the send chain).
 * @returns The cached (or freshly fetched) model config payload.
 */
export async function getModelConfigCached(): Promise<ModelConfig> {
  if (cached) return cached;
  try {
    cached = await fetchModelConfig();
  } catch {
    cached = invalidModelConfig();
  }
  return cached;
}

/**
 * Invalidate the cache. Called after ConfigDialog saves env changes
 * so the next getModelConfigCached() refetches from the backend.
 */
export function invalidateModelConfigCache(): void {
  cached = null;
}
