/** Client for the optional code-intel embedder download/enable endpoints. */

export type CodeEmbedderState = 'idle' | 'downloading' | 'downloaded' | 'ready' | 'error';

export interface CodeEmbedderStatus {
  profile: string;
  license: string;
  natural_languages: string[];
  size_bytes: number;
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  files_ready: boolean;
  enabled: boolean;
  state: CodeEmbedderState;
  error: string | null;
}

/** Read the current download/enable status. @returns The status payload, or null. */
export async function fetchCodeEmbedderStatus(): Promise<CodeEmbedderStatus | null> {
  return fetchApi<CodeEmbedderStatus>({
    url: '/code-embedder/status',
    opts: { _ts: Date.now() },
    method: 'get'
  });
}

/** Start the background download (and enable on completion). @returns The status payload, or null. */
export async function startCodeEmbedderDownload(): Promise<CodeEmbedderStatus | null> {
  return fetchApi<CodeEmbedderStatus>({
    url: '/code-embedder/download',
    opts: {},
    method: 'post'
  });
}

/** Remove the enable marker without deleting the model files. @returns The status payload, or null. */
export async function disableCodeEmbedder(): Promise<CodeEmbedderStatus | null> {
  return fetchApi<CodeEmbedderStatus>({
    url: '/code-embedder',
    opts: {},
    method: 'delete'
  });
}
