<template>
  <div class="flex h-full min-h-0 flex-col">
    <div
      class="flex shrink-0 items-center gap-2 border-b border-solid border-gray-100 px-3 py-2 text-xs dark:border-gray-800">
      <i class="pi pi-file text-gray-400"></i>
      <span class="truncate font-mono text-gray-500 dark:text-gray-400">{{ pathLabel }}</span>
    </div>

    <div class="min-h-0 flex-1 overflow-auto p-3">
      <div
        v-if="loading"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>
      <p
        v-else-if="error"
        class="m-0 break-all text-xs text-red-500 dark:text-red-400"
        data-test="file-viewer-error">
        {{ error }}
      </p>
      <!-- One branch per preview format: markdown keeps the shared sanitizing
           directive, PDF / docx delegate to their viewers (which fetch nothing
           themselves — the panel hands them the cached bytes), images render an
           object URL with the global click-to-zoom overlay, and everything else
           falls back to the plain <pre>. -->
      <div
        v-else-if="fileFormat === 'markdown'"
        class="markdown-body text-sm"
        data-test="file-viewer-md"
        v-safe-html="content"></div>
      <PdfViewer
        v-else-if="fileFormat === 'pdf' && blobEntry"
        :bytes="blobEntry.bytes"
        data-test="file-viewer-pdf" />
      <DocxViewer
        v-else-if="fileFormat === 'docx' && blobEntry"
        :bytes="blobEntry.bytes"
        data-test="file-viewer-docx" />
      <div
        v-else-if="fileFormat === 'image' && blobEntry"
        class="flex h-full items-center justify-center"
        data-test="file-viewer-image">
        <img
          v-if="imageUrl"
          :src="imageUrl"
          :alt="pathLabel"
          class="max-h-full max-w-full cursor-zoom-in object-contain"
          @click="openPreview(imageUrl)" />
      </div>
      <div
        v-else-if="fileFormat === 'code' && codeHtml"
        class="text-xs"
        data-test="file-viewer-code"
        v-safe-html.fragment="codeHtml"></div>
      <pre
        v-else
        class="m-0 whitespace-pre-wrap break-words font-mono text-xs"
        data-test="file-viewer-pre"
        >{{ content }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';
import { classifyFileFormat } from '~/utils/file-format';
import type { FileViewerEntry } from '~/stores/file-viewer';

/** Files above this size keep the plain <pre> (highlight cost + DOM weight). */
const MAX_CODE_HIGHLIGHT_BYTES = 256 * 1024;
import PdfViewer from './PdfViewer.vue';
import DocxViewer from './DocxViewer.vue';

const props = defineProps<{
  /** The tab's payload: the file's path relative to the project root. */
  payload?: { path: string };
  /** Session whose project root the file belongs to (page-provided). */
  sessionId?: string;
}>();

const { t } = useI18n();
const store = useFileViewerStore();
const projectDirectory = useProjectDirectoryStore();
const route = useRoute();

const content = ref('');
const size = ref(0);
const loading = ref(false);
const error = ref<string | null>(null);
/** The Shiki-highlighted HTML for a `code`-format file (empty → the plain <pre>). */
const codeHtml = ref('');
/** The binary entry behind the pdf / docx / image branches (null for text). */
const blobEntry = ref<{ bytes: ArrayBuffer; mime: string } | null>(null);
/** The image branch's object URL — created per entry, revoked on every change. */
const imageUrl = ref('');

const pathLabel = computed(() => props.payload?.path ?? '');
const fileFormat = computed(() => classifyFileFormat(pathLabel.value));
/** The session this panel belongs to: the prop, else the route's sid. */
const effectiveSession = computed(() => props.sessionId ?? String(route.params.sid ?? ''));
/** The formats fetched as bytes (markdown / text keep the text endpoint). */
const BLOB_FORMATS = new Set(['pdf', 'docx', 'image']);

/** The global click-to-zoom overlay (ImagePreviewOverlay, mounted in app.vue). */
const { openPreview } = useImagePreview();

function revokeImageUrl(): void {
  if (imageUrl.value) {
    URL.revokeObjectURL(imageUrl.value);
    imageUrl.value = '';
  }
}

function applyEntry(entry: FileViewerEntry): void {
  revokeImageUrl();
  codeHtml.value = '';
  if (entry.kind === 'text') {
    content.value = entry.content;
    size.value = entry.size;
    blobEntry.value = null;
    if (fileFormat.value === 'code' && entry.size <= MAX_CODE_HIGHLIGHT_BYTES) {
      const ext = pathLabel.value.split('.').pop()?.toLowerCase() ?? '';
      void highlightCode(entry.content, ext).then(highlighted => {
        if (highlighted) codeHtml.value = highlighted;
      });
    }
    return;
  }
  content.value = '';
  size.value = entry.size;
  blobEntry.value = { bytes: entry.bytes, mime: entry.mime };
  if (fileFormat.value === 'image') {
    imageUrl.value = URL.createObjectURL(new Blob([entry.bytes], { type: entry.mime }));
  }
}

async function load(): Promise<void> {
  const sid = effectiveSession.value;
  const path = props.payload?.path;
  if (!sid || !path) {
    error.value = t('projectFiles.noFile');
    return;
  }
  const cached = store.get(sid, path);
  if (cached) {
    applyEntry(cached);
    error.value = null;
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    if (BLOB_FORMATS.has(fileFormat.value)) {
      const { bytes, mime } = await fetchProjectFileBlob(sid, path);
      const entry: FileViewerEntry = {
        kind: 'blob',
        bytes,
        mime,
        size: bytes.byteLength,
        fetchedAt: Date.now()
      };
      store.put(sid, path, { kind: 'blob', bytes, mime, size: bytes.byteLength });
      applyEntry(entry);
    } else {
      const file = await fetchProjectFile(sid, path);
      content.value = file.content;
      size.value = file.size;
      store.put(sid, path, file.content, file.size);
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    logUtil.w('[FileViewerPanel] load failed:', error.value);
  } finally {
    loading.value = false;
  }
}

watch([() => props.payload?.path, effectiveSession], () => void load(), { immediate: true });

// A project switch makes every cached relative path stale.
watch(
  () => (effectiveSession.value ? projectDirectory.stateFor(effectiveSession.value).directory : null),
  () => store.clear()
);

onScopeDispose(revokeImageUrl);
</script>
