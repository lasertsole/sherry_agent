/**
 * First-run backend download state (packaged desktop app).
 *
 * A packaged build ships only the front end + the Rust shell: the Python backend
 * tree is downloaded from the repository's GitHub releases the first time the app
 * runs, and again after an installer upgrade that changes the version
 * (`client/src-tauri/src/services/backend_downloader.rs`). The shell drives that
 * download and reports it through Tauri events; this composable mirrors those
 * events into reactive state for the overlay and exposes the retry its failure
 * card needs.
 *
 * Two rules keep it honest:
 *
 * - The shell starts the download in its `setup()` hook — before the SPA mounts —
 *   and events are broadcast, not queued. The state is therefore PULLED once over
 *   IPC on init, and the events only take over from there.
 * - Outside the Tauri runtime (a browser build) there is no shell and no backend
 *   download: every call resolves to the idle state and nothing renders.
 */

import { ref } from 'vue';

import type { BackendDownloadStatus } from '~/types/backend/backend-download-status';

/** `idle` (no download) / `running` / `ready` (backend up) / `failed`. */
export type BackendDownloadState = 'idle' | 'running' | 'ready' | 'failed';

const state = ref<BackendDownloadState>('idle');
const percent = ref(0);
const downloadedBytes = ref(0);
const totalBytes = ref(0);
const error = ref('');

let started = false;

function applyStatus(status: BackendDownloadStatus | null): void {
  if (!status) return;
  // A snapshot reading `ready` means the download finished BEFORE this page
  // mounted — the shell keeps that state, it does not reset it. The backend is
  // simply up, so there is nothing to show and, in particular, nothing to
  // re-load for: only an event ("it just finished") drives the hand-off.
  state.value = status.state === 'ready' ? 'idle' : (status.state as BackendDownloadState) || 'idle';
  percent.value = status.percent;
  downloadedBytes.value = status.downloaded;
  totalBytes.value = status.total;
  error.value = status.error ?? '';
}

/**
 * Start mirroring the shell's download state (idempotent).
 *
 * Outside the Tauri runtime the bridge calls resolve to "nothing happened", so
 * the state stays `idle` and no overlay renders.
 * @returns When the initial snapshot and the event subscription are in place.
 */
export async function initBackendDownload(): Promise<void> {
  if (started) return;
  started = true;

  applyStatus(await fetchBackendDownloadStatus().catch(() => null));
  await subscribeBackendDownload({
    onStarted: () => {
      state.value = 'running';
      error.value = '';
    },
    onProgress: progress => {
      state.value = 'running';
      percent.value = progress.percent;
      downloadedBytes.value = progress.downloaded;
      totalBytes.value = progress.total;
    },
    onComplete: () => {
      state.value = 'ready';
      percent.value = 100;
    },
    onError: message => {
      state.value = 'failed';
      error.value = message;
    }
  }).catch(() => {
    // A shell without the event channel (an older build) still answers the pull.
  });
}

/**
 * The shared download state for the overlay.
 * @returns The reactive state plus the retry behind the failure card.
 */
export function useBackendDownload() {
  return {
    state,
    percent,
    downloadedBytes,
    totalBytes,
    error,
    /** Ask the shell to try again; the failure message stays on screen on a new failure. */
    async retry(): Promise<void> {
      error.value = '';
      state.value = 'running';
      try {
        await retryBackendDownload();
      } catch (e) {
        state.value = 'failed';
        error.value = String(e);
      }
    }
  };
}
