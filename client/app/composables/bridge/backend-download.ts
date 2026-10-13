/**
 * Backend download bridge calls (packaged desktop app, first run).
 *
 * The installer ships only the front end + the Rust shell, so the Python backend
 * is downloaded from the repository's GitHub releases the first time the app
 * runs and again after an installer upgrade
 * (`client/src-tauri/src/services/backend_downloader.rs`). The shell drives that
 * download; these three calls are the client half:
 *
 * - `fetchBackendDownloadStatus` — the pull half, for a front end that mounted
 *   after the shell had already started (the events are broadcast, not queued);
 * - `subscribeBackendDownload` — the push half (Tauri events → handlers);
 * - `retryBackendDownload` — behind the failure card.
 *
 * Every call is a no-op outside the Tauri runtime (a browser build has no shell
 * to download anything, and a `tauri dev` run uses the checkout backend).
 *
 * @module bridge/backend-download
 */

import { getListen, invokeNative, isTauri } from './transport';
import type { BackendDownloadStatus } from '~/types/backend/backend-download-status';
import type { DownloadProgress } from '~/types/backend/download-progress';

/** Handlers the shell's download events are delivered to. */
export interface BackendDownloadHandlers {
  /** The transfer is starting. */
  onStarted: () => void;
  /** A throttled progress report. */
  onProgress: (progress: DownloadProgress) => void;
  /** The backend is downloaded, installed AND running. */
  onComplete: () => void;
  /** The download, the extraction or the launch failed. */
  onError: (message: string) => void;
}

/**
 * The download's current state.
 * @returns The snapshot, or null outside the Tauri runtime.
 */
export async function fetchBackendDownloadStatus(): Promise<BackendDownloadStatus | null> {
  const result = await invokeNative<BackendDownloadStatus>('backend_download_status');
  return result ? result.value : null;
}

/**
 * Ask the shell to (re)start the download.
 *
 * The command's own failure message propagates.
 * @returns `true` when a retry was requested; `false` outside the Tauri runtime
 * (there is no shell to retry).
 */
export async function retryBackendDownload(): Promise<boolean> {
  const result = await invokeNative<null>('retry_backend_download');
  return result !== null;
}

/**
 * Subscribe to the shell's download events.
 * @param handlers Handlers for the four download events.
 * @returns An unsubscribe function that detaches every listener (always safe to
 * call, also outside the Tauri runtime).
 */
export async function subscribeBackendDownload(handlers: BackendDownloadHandlers): Promise<() => void> {
  if (!isTauri()) return () => {};
  const listen = await getListen();
  const unlisten = await Promise.all([
    listen('backend-download-started', () => handlers.onStarted()),
    listen<DownloadProgress>('backend-download-progress', event => handlers.onProgress(event.payload)),
    listen('backend-download-complete', () => handlers.onComplete()),
    listen<{ error: string }>('backend-download-error', event =>
      handlers.onError(event.payload?.error ?? 'backend download failed')
    )
  ]);
  return () => unlisten.forEach(detach => detach());
}
