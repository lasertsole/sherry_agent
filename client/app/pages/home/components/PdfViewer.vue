<template>
  <div
    class="flex h-full min-h-0 flex-col"
    data-test="pdf-viewer">
    <div
      class="flex shrink-0 items-center justify-center gap-2 border-b border-solid border-gray-100 py-1 text-xs text-gray-500 dark:border-gray-800 dark:text-gray-400">
      <Button
        icon="pi pi-chevron-up"
        text
        rounded
        severity="secondary"
        :aria-label="t('projectFiles.viewerPdfPrev')"
        :disabled="page <= 1 || rendering"
        data-test="pdf-prev"
        @click="goTo(page - 1)" />
      <span
        class="tabular-nums"
        data-test="pdf-page-label">
        {{ t('projectFiles.viewerPdfPage', { n: page, total: clampedNumPages }) }}
      </span>
      <Button
        icon="pi pi-chevron-down"
        text
        rounded
        severity="secondary"
        :aria-label="t('projectFiles.viewerPdfNext')"
        :disabled="page >= clampedNumPages || rendering"
        data-test="pdf-next"
        @click="goTo(page + 1)" />
      <Button
        icon="pi pi-arrows-h"
        text
        rounded
        severity="secondary"
        :aria-label="t('projectFiles.viewerPdfFit')"
        data-test="pdf-fit"
        @click="fitWidth()" />
    </div>
    <p
      v-if="truncated"
      class="m-0 shrink-0 bg-amber-50 px-3 py-1 text-xs text-amber-600 dark:bg-amber-900/20 dark:text-amber-400"
      data-test="pdf-truncated">
      {{ t('projectFiles.viewerPdfTooMany', { n: MAX_PDF_PAGES }) }}
    </p>
    <div
      ref="containerRef"
      class="min-h-0 flex-1 overflow-auto bg-gray-50 p-2 dark:bg-gray-900">
      <div class="flex justify-center">
        <canvas
          ref="canvasRef"
          class="max-w-full bg-white shadow-sm"
          data-test="pdf-canvas"></canvas>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * PDF viewer: pdf.js renders one page at a time onto a single reused canvas.
 *
 * pdf.js TRANSFERS the passed buffer to its worker (neutering the caller's
 * ArrayBuffer), so the viewer always hands it a copy — the file-viewer store's
 * cached bytes must survive a page turn. The worker is the bundled `?url`
 * asset: it loads from `self`, which the Tauri CSP's `worker-src` already
 * allows. The import is lazy (pdf.js is ~1 MB) and the module keeps a render
 * guard so rapid page flips never stack draws onto the same canvas.
 */
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { PDFDocumentLoadingTask, PDFDocumentProxy } from 'pdfjs-dist';
import { logUtil } from '~/utils/log';

/** Pages rendered before the viewer truncates (a 2 000-page scan is not a preview). */
const MAX_PDF_PAGES = 200;

const props = defineProps<{
  /** The file's raw bytes (the viewer copies before handing them to pdf.js). */
  bytes: ArrayBuffer;
}>();

const { t } = useI18n();
const canvasRef = useTemplateRef<HTMLCanvasElement>('canvasRef');
const containerRef = useTemplateRef<HTMLElement>('containerRef');

const numPages = ref(0);
const page = ref(1);
const rendering = ref(false);
const scale = ref(1);
const fitApplied = ref(false);
let pdfDoc: PDFDocumentProxy | null = null;
// v6 moves destroy() to the loading task; the proxy no longer carries it.
let loadingTask: PDFDocumentLoadingTask | null = null;
let renderTask: { cancel: () => void; promise: Promise<void> } | null = null;
let generation = 0;

/** The page count the toolbar shows (the truncation cap is applied up front). */
const clampedNumPages = computed(() => Math.min(numPages.value, MAX_PDF_PAGES));
const truncated = computed(() => numPages.value > MAX_PDF_PAGES);

type PdfModule = typeof import('pdfjs-dist');
async function loadPdf(): Promise<PdfModule> {
  const pdfjs = await import('pdfjs-dist');
  if (!pdfjs.GlobalWorkerOptions.workerSrc) {
    // Static ?url import: Vite emits the worker file and resolves its URL.
    const workerUrl = (await import('pdfjs-dist/build/pdf.worker.min.mjs?url')).default;
    pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  }
  return pdfjs;
}

async function renderCurrent(): Promise<void> {
  const canvas = canvasRef.value;
  if (!pdfDoc || !canvas) return;
  const pageNumber = page.value;
  const myGeneration = ++generation;
  renderTask?.cancel();
  rendering.value = true;
  try {
    const pdfPage = await pdfDoc.getPage(pageNumber);
    if (myGeneration !== generation) return;
    if (fitApplied.value && containerRef.value) {
      const base = pdfPage.getViewport({ scale: 1 });
      const available = containerRef.value.clientWidth - 16;
      scale.value = Math.min(3, Math.max(0.5, available / base.width));
    }
    const viewport = pdfPage.getViewport({ scale: scale.value });
    const context = canvas.getContext('2d');
    if (!context || myGeneration !== generation) return;
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    const task = pdfPage.render({ canvas: canvas, canvasContext: context, viewport });
    renderTask = task;
    await task.promise;
  } catch (error) {
    // A cancelled render rejects with its own exception type — that is the
    // rapid-flip path, not a failure. Anything else surfaces as an empty page.
    if (myGeneration === generation) {
      logUtil.w('[PdfViewer] render failed:', error);
    }
  } finally {
    if (myGeneration === generation) {
      rendering.value = false;
      renderTask = null;
    }
  }
}

async function goTo(next: number): Promise<void> {
  if (next < 1 || next > clampedNumPages.value) return;
  page.value = next;
  await renderCurrent();
}

function fitWidth(): void {
  fitApplied.value = true;
  void renderCurrent();
}

onMounted(async () => {
  try {
    const pdfjs = await loadPdf();
    // Copy: pdf.js transfers (and neuters) the buffer it receives, and the
    // store's cached bytes belong to the file-viewer cache, not to pdf.js.
    loadingTask = pdfjs.getDocument({ data: new Uint8Array(props.bytes.slice(0)) });
    pdfDoc = await loadingTask.promise;
    numPages.value = pdfDoc.numPages;
    fitApplied.value = true;
    await renderCurrent();
  } catch (error) {
    logUtil.w('[PdfViewer] load failed:', error);
    numPages.value = 0;
  }
});

// A new byte payload (the panel reuses the component across tab switches only
// via remount, but keep the contract honest anyway).
watch(
  () => props.bytes,
  () => {
    void loadingTask?.destroy();
    loadingTask = null;
    pdfDoc = null;
    numPages.value = 0;
    page.value = 1;
  }
);

onScopeDispose(() => {
  generation++;
  renderTask?.cancel();
  void loadingTask?.destroy();
  loadingTask = null;
  pdfDoc = null;
});
</script>
