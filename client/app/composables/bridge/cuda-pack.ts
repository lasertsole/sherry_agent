/**
 * Optional CUDA pack bridge (Tauri desktop only).
 *
 * The unified installer ships the CPU backend; the CUDA acceleration payload
 * is a separate, user-opted release asset the shell downloads in the
 * background (`client/src-tauri/src/services/cuda_pack_downloader.rs`).
 *
 * - `fetchCudaSupport` — the model settings tab's visibility gate (no NVIDIA
 *   driver → no card at all);
 * - `fetchCudaPackStatus` — the pull half, for a front end that mounted after
 *   the download had already started (the events are broadcast, not queued);
 * - `subscribeCudaPackDownload` — the push half (Tauri events → handlers);
 * - `downloadCudaPack` / `removeCudaPack` — the card's two lifecycle buttons;
 * - `restartBackend` — the "install and restart" hand-off (torch resolves the
 *   CUDA libraries at import, so a running backend picks the pack up only on
 *   a fresh process).
 *
 * Every call is a no-op / null outside the Tauri runtime (a browser build has
 * no shell and the model settings tab hides the card).
 *
 * @module bridge/cuda-pack
 */
import { getListen, invokeNative, isTauri } from './transport';
import type { CudaSupport } from '~/types/backend/cuda-support';
import type { CudaPackStatus } from '~/types/backend/cuda-pack-status';
import type { DownloadProgress } from '~/types/backend/download-progress';

/** Handlers the shell's CUDA pack download events are delivered to. */
export interface CudaPackDownloadHandlers {
  /** The transfer is starting. */
  onStarted: () => void;
  /** A throttled progress report (Range resume counts prior bytes in). */
  onProgress: (progress: DownloadProgress) => void;
  /** The pack is downloaded AND installed. */
  onComplete: () => void;
  /** The download or the extraction failed. */
  onError: (message: string) => void;
}

/**
 * Probe the host's CUDA support (the card's visibility gate).
 * @returns The support verdict, or null outside the Tauri runtime.
 */
export async function fetchCudaSupport(): Promise<CudaSupport | null> {
  const result = await invokeNative<CudaSupport>('cuda_support');
  return result ? result.value : null;
}

/**
 * The CUDA pack download's current state.
 * @returns The status, or null outside the Tauri runtime.
 */
export async function fetchCudaPackStatus(): Promise<CudaPackStatus | null> {
  const result = await invokeNative<CudaPackStatus>('cuda_pack_status');
  return result ? result.value : null;
}

/**
 * Ask the shell to start (or resume) the pack download in the background.
 * @returns `true` when a download was requested; `false` outside the Tauri runtime.
 */
export async function downloadCudaPack(): Promise<boolean> {
  const result = await invokeNative<null>('download_cuda_pack');
  return result !== null;
}

/**
 * Remove the installed pack; the backend falls back to CPU on its next start.
 */
export async function removeCudaPack(): Promise<void> {
  await invokeNative<null>('remove_cuda_pack');
}

/** Stop and restart the managed backend (the pack takes effect on a fresh process). */
export async function restartBackend(): Promise<void> {
  await invokeNative<null>('restart_backend');
}

/**
 * Subscribe to the shell's CUDA pack events.
 * @param handlers Handlers for the four download events.
 * @returns An unsubscribe function that detaches every listener (always safe to
 * call, also outside the Tauri runtime).
 */
export async function subscribeCudaPackDownload(handlers: CudaPackDownloadHandlers): Promise<() => void> {
  if (!isTauri()) return () => {};
  const listen = await getListen();
  const unlisten = await Promise.all([
    listen('cuda-pack-download-started', () => handlers.onStarted()),
    listen<DownloadProgress>('cuda-pack-download-progress', event => handlers.onProgress(event.payload)),
    listen('cuda-pack-download-complete', () => handlers.onComplete()),
    listen<{ error: string }>('cuda-pack-download-error', event =>
      handlers.onError(event.payload?.error ?? 'CUDA pack download failed')
    )
  ]);
  return () => unlisten.forEach(detach => detach());
}
