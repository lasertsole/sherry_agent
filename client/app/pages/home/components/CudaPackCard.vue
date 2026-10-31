<script setup lang="ts">
/**
 * The model settings tab's optional CUDA pack card (Tauri desktop, Linux).
 *
 * Visibility is the shell's detection: no NVIDIA driver → nothing renders at
 * all. With support, the card shows the pack's lifecycle — download (with
 * Range-resume progress), "install and restart", uninstall — and the reason
 * line when the host cannot accelerate. The web build never sees this card
 * (the composable resolves to null there).
 */
import Button from 'primevue/button';
import ProgressBar from 'primevue/progressbar';
import { useI18n } from 'vue-i18n';

// Explicit import (not the auto-import): the component tests mount this card
// without the unimport injection, so a bare symbol would be undefined there.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { useCudaPack } from '~/composables/use-cuda-pack';

const {
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
} = useCudaPack();

const { t } = useI18n();

/**
 * "1.8 GiB / 4.5 GiB" for the progress line (0 hides the size half).
 * @param bytes
 */
function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MiB`;
  return `${(bytes / 1024).toFixed(0)} KiB`;
}

const sizeLine = computed(() =>
  totalBytes.value > 0 ? `${formatBytes(downloadedBytes.value)} / ${formatBytes(totalBytes.value)}` : ''
);
</script>

<template>
  <div
    v-if="offerCard"
    class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3"
    data-test="cuda-pack">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="m-0 text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('config.cuda.title') }}
        </p>
        <p class="m-0 mt-1 text-xs text-gray-500 dark:text-gray-400">
          {{ t('config.cuda.hint') }}
        </p>
        <p
          v-if="cudaSupport"
          class="m-0 mt-1 text-xs"
          :class="cudaSupport.supported ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-400'">
          <template v-if="cudaSupport.supported">
            {{
              t('config.cuda.supported', { gpu: cudaSupport.gpu_name ?? '', driver: cudaSupport.driver_version ?? '' })
            }}
          </template>
          <template v-else>
            {{ cudaSupport.reason || t('config.cuda.unsupported') }}
          </template>
        </p>
        <p
          v-if="error"
          class="m-0 mt-1 text-xs text-red-600 dark:text-red-400">
          {{ error }}
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <Button
          v-if="packState !== 'running'"
          size="small"
          severity="secondary"
          text
          :label="t('config.cuda.uninstall')"
          :loading="busy"
          @click="uninstallCudaPack" />
        <Button
          v-if="packInstalled"
          size="small"
          icon="pi pi-refresh"
          :label="t('config.cuda.restart')"
          :loading="busy"
          @click="restartBackendForCudaPack" />
        <Button
          v-else-if="packState !== 'running'"
          size="small"
          icon="pi pi-download"
          :label="t('config.cuda.download')"
          :loading="busy"
          @click="startCudaPackDownload" />
      </div>
    </div>
    <ProgressBar
      v-if="packState === 'running' || packInstalled"
      :value="percent"
      :show-value="false"
      class="h-2" />
    <p
      v-if="packState === 'running' || packInstalled"
      class="m-0 text-xs text-gray-400">
      {{ sizeLine }} · {{ percent }}%
      <span v-if="packInstalled">{{ t('config.cuda.installedHint') }}</span>
    </p>
  </div>
</template>
