/** Client for the local-model prefetch progress endpoints (模型配置). */

export type ModelDownloadPhase = 'idle' | 'downloading' | 'ready' | 'error';

export interface LocalModelDownload {
  label: string;
  state: ModelDownloadPhase;
  ready: boolean;
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  files_done: number;
  file_count: number;
  files: Array<{ name: string; exists: boolean; fallback_ready: boolean }>;
  error: string | null;
}

export interface ModelDownloadsStatus {
  models: Record<string, LocalModelDownload>;
}

/** Read all local-model progress states. */
export async function fetchModelDownloadsStatus(): Promise<ModelDownloadsStatus | null> {
  return fetchApi<ModelDownloadsStatus>({
    url: '/model-downloads/status',
    opts: { _ts: Date.now() },
    method: 'get'
  });
}

/**
 * Start one family's prefetch; returns that family's state.
 * @param family
 */
export async function startModelDownload(family: string): Promise<LocalModelDownload | null> {
  return fetchApi<LocalModelDownload>({
    url: '/model-downloads/download',
    opts: { family },
    method: 'post'
  });
}
