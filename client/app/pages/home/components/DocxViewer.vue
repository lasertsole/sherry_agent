<template>
  <div class="min-h-0">
    <p
      v-if="failed"
      class="m-0 text-xs text-red-500 dark:text-red-400"
      data-test="docx-error">
      {{ t('projectFiles.viewerDocxFailed') }}
    </p>
    <div
      v-else
      class="docx-body text-sm"
      data-test="docx-viewer"
      v-safe-html.fragment="html"></div>
  </div>
</template>

<script setup lang="ts">
/**
 * docx viewer: mammoth converts the OOXML into semantic HTML, which renders
 * through the `v-safe-html.fragment` path (DOMPurify allowlist, NO markdown
 * pass — mammoth's block structures must not be re-parsed). The pre-sanitized
 * string is what the directive receives; the directive sanitizes again, which
 * is idempotent for already-allowlisted markup and keeps the raw-HML rule
 * (`vue/no-v-html: error`) satisfied with exactly one rendering channel.
 *
 * mammoth's main entry (`lib/index.js`) needs Node builtins, so the import
 * targets its prebuilt browser bundle directly — Vite handles the UMD
 * interop and the lazy import keeps it out of the main chunk.
 */
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';

const { t } = useI18n();

const props = defineProps<{
  /** The file's raw bytes (an OOXML zip; mammoth owns the parsing). */
  bytes: ArrayBuffer;
}>();

const html = ref('');
const failed = ref(false);

async function convert(bytes: ArrayBuffer): Promise<void> {
  html.value = '';
  failed.value = false;
  try {
    const mammoth = (await import('mammoth/mammoth.browser.js')).default;
    const result = await mammoth.convertToHtml({ arrayBuffer: bytes });
    html.value = result.value ?? '';
  } catch (error) {
    logUtil.w('[DocxViewer] convert failed:', error);
    failed.value = true;
  }
}

watch(
  () => props.bytes,
  bytes => void convert(bytes),
  { immediate: true }
);
</script>

<style scoped>
/* Minimal document typography: mammoth emits semantic HTML with no styling. */
.docx-body :deep(p) {
  margin: 0.5rem 0;
}
.docx-body :deep(h1),
.docx-body :deep(h2),
.docx-body :deep(h3) {
  margin: 0.75rem 0 0.5rem;
  font-weight: 600;
}
.docx-body :deep(table) {
  border-collapse: collapse;
  margin: 0.5rem 0;
}
.docx-body :deep(td),
.docx-body :deep(th) {
  border: 1px solid rgb(203 213 225);
  padding: 0.25rem 0.5rem;
}
.docx-body :deep(img) {
  max-width: 100%;
  height: auto;
}
.docx-body :deep(blockquote) {
  border-left: 2px solid rgb(203 213 225);
  margin: 0.5rem 0;
  padding-left: 0.75rem;
  color: rgb(100 116 139);
}
</style>
