/** Client for the local-model prefetch progress endpoints (Model Config). */

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

/** One `model_download_updated` WS frame (a status snapshot for one target). */
export interface ModelDownloadUpdate<T = LocalModelDownload> {
  content?: {
    kind?: 'local' | 'code_embedder';
    family?: string;
    /** The same shape `/model-downloads/status` and `/code-embedder/status` return. */
    status?: T;
  };
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
