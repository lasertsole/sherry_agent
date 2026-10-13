<template>
  <Teleport to="body">
    <Transition name="backend-download-fade">
      <div
        v-if="state !== 'idle'"
        class="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm"
        role="dialog"
        aria-modal="true"
        :aria-label="t('title')">
        <div
          class="w-full max-w-md rounded-xl bg-white p-6 text-center shadow-2xl dark:bg-[#1e1e28] dark:text-slate-100">
          <h2 class="text-base font-semibold">{{ t('title') }}</h2>

          <!-- Ready: the backend was downloaded, installed and started — the SPA
               re-boots so every store refetches against a live backend. -->
          <template v-if="state === 'ready'">
            <p class="mt-3 text-sm text-slate-500 dark:text-slate-400">{{ t('starting') }}</p>
          </template>

          <template v-else>
            <p class="mt-3 text-sm text-slate-500 dark:text-slate-400">{{ t('hint') }}</p>

            <div
              class="mt-4 h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
              role="progressbar"
              :aria-valuenow="percent"
              aria-valuemin="0"
              aria-valuemax="100">
              <div
                class="h-full rounded-full bg-sky-500 transition-[width] duration-200"
                :style="{ width: `${percent}%` }" />
            </div>

            <p class="mt-2 text-xs tabular-nums text-slate-500 dark:text-slate-400">
              {{ progressText }}
            </p>

            <!-- Failure: the card stays up (nothing else can work without the
                 backend) and offers the retry. -->
            <template v-if="state === 'failed'">
              <p
                class="mt-3 break-words text-xs text-red-600 dark:text-red-400"
                data-test="backend-download-error">
                {{ error || t('failed') }}
              </p>
              <button
                type="button"
                class="mt-4 rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700"
                data-test="backend-download-retry"
                @click="retry">
                {{ t('retry') }}
              </button>
            </template>
          </template>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<i18n lang="json">
{
  "en": {
    "title": "Downloading the Sherry backend",
    "hint": "First launch only: the backend components are being downloaded. The app is ready as soon as this finishes.",
    "starting": "Backend ready — starting it now…",
    "retry": "Retry download",
    "failed": "Download failed."
  },
  "ja": {
    "title": "Sherry バックエンドをダウンロード中",
    "hint": "初回起動のみ：バックエンドをダウンロードしています。完了するとすぐに使えます。",
    "starting": "バックエンドの準備ができました — 起動しています…",
    "retry": "ダウンロードを再試行",
    "failed": "ダウンロードに失敗しました。"
  },
  "ko": {
    "title": "Sherry 백엔드 다운로드 중",
    "hint": "최초 실행 시에만: 백엔드 구성 요소를 내려받고 있습니다. 완료되면 바로 사용할 수 있습니다.",
    "starting": "백엔드 준비 완료 — 시작하는 중…",
    "retry": "다운로드 다시 시도",
    "failed": "다운로드에 실패했습니다."
  },
  "zh": {
    "title": "正在下载 Sherry 后端",
    "hint": "仅首次启动需要：正在下载后端组件，完成后即可使用。",
    "starting": "后端已就绪，正在启动…",
    "retry": "重试下载",
    "failed": "下载失败。"
  }
}
</i18n>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n({ useScope: 'local' });

const { state, percent, downloadedBytes, totalBytes, error, retry } = useBackendDownload();

/** How long the "backend ready" state stays up before the app re-boots. */
const RESTART_DELAY_MS = 800;

const restartTimer = ref<ReturnType<typeof setTimeout> | null>(null);

/**
 * Bytes as whole megabytes (the progress line is a rough read-out, not a metric).
 * @param bytes Byte count from a progress event.
 * @returns Whole megabytes.
 */
const toMegabytes = (bytes: number) => Math.round(bytes / 1024 / 1024);

const progressText = computed(() => {
  const done = toMegabytes(downloadedBytes.value);
  const total = toMegabytes(totalBytes.value);
  return total > 0 ? `${done} / ${total} MB (${percent.value}%)` : `${done} MB (${percent.value}%)`;
});

onMounted(() => {
  void initBackendDownload();
});

// The download finishing means the backend is running, but this SPA booted while
// it was not: its first fetches failed and its sockets are retrying. Re-loading
// the page is the one reliable way to hand it a clean start (this happens once
// per install/upgrade).
watch(state, value => {
  if (value !== 'ready' || restartTimer.value) return;
  restartTimer.value = setTimeout(() => {
    if (import.meta.client) window.location.reload();
  }, RESTART_DELAY_MS);
});
</script>

<style scoped>
.backend-download-fade-enter-active,
.backend-download-fade-leave-active {
  transition: opacity 0.25s ease;
}

.backend-download-fade-enter-from,
.backend-download-fade-leave-to {
  opacity: 0;
}
</style>
