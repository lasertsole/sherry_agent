/**
 * Optional CUDA pack state (the model settings tab's download card).
 *
 * The unified installer ships the CPU backend; the CUDA acceleration payload
 * is a separate Linux-only release asset the user opts into from the model
 * settings tab (`client/src-tauri/src/services/cuda_pack_downloader.rs`).
 * This composable mirrors the shell's detection + download state into
 * reactive refs and exposes the card's actions.
 *
 * Two rules keep it honest (the same shape as `use-backend-download`):
 *
 * - The state is PULLED once over IPC on init and the Tauri events take over
 *   from there — events are broadcast, not queued.
 * - Outside the Tauri runtime (a browser build) every call resolves to null /
 *   idle and the card never renders.
 */

import { computed, onMounted, onUnmounted, ref } from 'vue';

import type { CudaSupport } from '~/types/backend/cuda-support';
// The composable needs a stable module specifier so tests can vi.mock the
// bridge; the unimport injection is compile-time and leaves bare symbols
// unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import {
  downloadCudaPack,
  fetchCudaPackStatus,
  fetchCudaSupport,
  removeCudaPack,
  restartBackend,
  subscribeCudaPackDownload
} from './bridge/cuda-pack';

/** `idle` (no pack) / `running` / `ready` / `failed`. */
export type CudaPackState = 'idle' | 'running' | 'ready' | 'failed';

const cudaSupport = ref<CudaSupport | null>(null);
const packState = ref<CudaPackState>('idle');
const percent = ref(0);
const downloadedBytes = ref(0);
const totalBytes = ref(0);
const error = ref('');
const busy = ref(false);

let started = false;
let unsubscribe: (() => void) | null = null;

/** Whether the card renders at all: Tauri desktop + a supported NVIDIA host. */
const offerCard = computed(() => cudaSupport.value?.supported === true || packState.value !== 'idle');
/** Whether a usable pack is installed (its "uninstall" button). */
const packInstalled = computed(() => packState.value === 'ready');

function applyStatus(
  status: { state: string; downloaded: number; total: number; percent: number; error: string | null } | null
): void {
  if (!status) return;
  packState.value = (status.state as CudaPackState) || 'idle';
  percent.value = status.percent;
  downloadedBytes.value = status.downloaded;
  totalBytes.value = status.total;
  error.value = status.error ?? '';
}

/**
 * Start mirroring the shell's CUDA state (idempotent).
 *
 * Outside the Tauri runtime nothing resolves and the card stays hidden.
 */
export async function initCudaPack(): Promise<void> {
  if (started) return;
  started = true;

  cudaSupport.value = await fetchCudaSupport().catch(() => null);
  applyStatus(await fetchCudaPackStatus().catch(() => null));
  unsubscribe = await subscribeCudaPackDownload({
    onStarted: () => {
      packState.value = 'running';
      error.value = '';
    },
    onProgress: progress => {
      packState.value = 'running';
      percent.value = progress.percent;
      downloadedBytes.value = progress.downloaded;
      totalBytes.value = progress.total;
    },
    onComplete: () => {
      packState.value = 'ready';
      percent.value = 100;
    },
    onError: message => {
      packState.value = 'failed';
      error.value = message;
    }
  }).catch(() => null);
}

/** Start (or resume) the background download. */
export async function startCudaPackDownload(): Promise<void> {
  busy.value = true;
  try {
    await downloadCudaPack();
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}

/** Remove the installed pack (the backend falls back to CPU on its next start). */
export async function uninstallCudaPack(): Promise<void> {
  busy.value = true;
  try {
    await removeCudaPack();
    packState.value = 'idle';
    percent.value = 0;
    downloadedBytes.value = 0;
    totalBytes.value = 0;
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}

/** Stop and restart the backend so the freshly installed pack takes effect. */
export async function restartBackendForCudaPack(): Promise<void> {
  busy.value = true;
  try {
    await restartBackend();
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}

/** Bind the reactive state into a component (the card's setup hook). */
export function useCudaPack() {
  onMounted(() => {
    void initCudaPack();
  });
  onUnmounted(() => {
    unsubscribe?.();
    unsubscribe = null;
    started = false;
  });
  return {
    cudaSupport,
    packState,
    packInstalled,
    offerCard,
    percent,
    downloadedBytes,
    totalBytes,
    error,
    busy,
    startCudaPackDownload,
    uninstallCudaPack,
    restartBackendForCudaPack
  };
}
