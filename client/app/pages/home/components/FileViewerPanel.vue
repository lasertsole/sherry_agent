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
      <!-- Markdown renders as HTML (the shared sanitizing directive); everything
           else is a plain <pre> — no syntax highlighter in this build. -->
      <div
        v-else-if="isMarkdown"
        class="markdown-body text-sm"
        data-test="file-viewer-md"
        v-safe-html="content"></div>
      <pre
        v-else
        class="m-0 whitespace-pre-wrap break-words font-mono text-xs"
        data-test="file-viewer-pre"
        >{{ content }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';

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

const pathLabel = computed(() => props.payload?.path ?? '');
const isMarkdown = computed(() => pathLabel.value.toLowerCase().endsWith('.md'));

/** The session this panel belongs to: the prop, else the route's sid. */
const effectiveSession = computed(() => props.sessionId ?? String(route.params.sid ?? ''));

async function load(): Promise<void> {
  const sid = effectiveSession.value;
  const path = props.payload?.path;
  if (!sid || !path) {
    error.value = t('projectFiles.noFile');
    return;
  }
  const cached = store.get(sid, path);
  if (cached) {
    content.value = cached.content;
    size.value = cached.size;
    error.value = null;
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    const file = await fetchProjectFile(sid, path);
    content.value = file.content;
    size.value = file.size;
    store.put(sid, path, file.content, file.size);
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
</script>
