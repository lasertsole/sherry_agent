/**
 * The structured-output degradation panel's API bridge (系统配置 → 模型配置).
 *
 * One row per (provider, model) whose schema-bound subagents had to fall back
 * to a weaker structured-output strategy. The backend persists the level and
 * decays it after a re-probe interval; clearing a row forces the next spawn to
 * try the default strategy again.
 */
export interface ModelCapEntry {
  provider: string;
  model: string;
  /** Persisted level (0 = LangChain structured output, 1/2 = fallbacks). */
  level: number;
  /** The level the next spawn would use after the re-probe decay. */
  effective_level: number;
  reason: string;
  escalated_at: string;
  updated_at: string;
  reprobe_count: number;
}

export async function readModelCaps(): Promise<ModelCapEntry[]> {
  const res = await fetchApi<{ entries?: ModelCapEntry[] }>({
    url: `/sherry-config/model-caps?_ts=${Date.now()}`,
    method: 'get'
  });
  if (res === null || res === undefined) throw new Error('GET /sherry-config/model-caps failed');
  return Array.isArray(res.entries) ? res.entries : [];
}

export async function clearModelCap(provider: string, model: string): Promise<boolean> {
  const query = new URLSearchParams({ provider, model });
  const res = await fetchApi<{ success?: boolean }>({
    url: `/sherry-config/model-caps?${query.toString()}`,
    method: 'delete'
  });
  return res?.success === true;
}

export async function clearAllModelCaps(): Promise<boolean> {
  const res = await fetchApi<{ success?: boolean }>({
    url: '/sherry-config/model-caps',
    method: 'delete'
  });
  return res?.success === true;
}
