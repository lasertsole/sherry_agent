<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- Toolbar: current level path + manual refresh (the tree also re-roots by
         itself when the session's project directory changes). -->
    <div
      class="flex shrink-0 items-center gap-2 border-b border-solid border-gray-100 px-3 py-2 text-xs dark:border-gray-800">
      <i class="pi pi-folder text-theme-main"></i>
      <span class="truncate font-mono text-gray-500 dark:text-gray-400">{{ rootLabel }}</span>
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        class="ml-auto"
        :title="t('projectFiles.refresh')"
        :aria-label="t('projectFiles.refresh')"
        @click="reload" />
    </div>

    <div class="min-h-0 flex-1 overflow-auto p-2">
      <!-- Unbound session: never show the sherry checkout's own tree (an
           implementation leak with no value for the user). -->
      <div
        v-if="unbound"
        class="flex h-full flex-col items-center justify-center gap-2 px-4 text-center text-xs text-gray-500 dark:text-gray-400"
        data-test="project-tree-unbound">
        <i class="pi pi-exclamation-triangle text-lg text-amber-500"></i>
        <p class="m-0">{{ t('projectFiles.unbound') }}</p>
        <p class="m-0">{{ t('projectFiles.unboundHint') }}</p>
      </div>

      <div
        v-else-if="loading && !nodes.length"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>

      <p
        v-else-if="error"
        class="m-0 break-all px-2 text-xs text-red-500 dark:text-red-400"
        data-test="project-tree-error">
        {{ error }}
      </p>

      <template v-else>
        <FileTreeNode
          v-for="node in nodes"
          :key="node.path"
          :node="node"
          :depth="0"
          :session-id="sessionId"
          :loading-paths="loadingPaths"
          @toggle="toggleNode"
          @open="openFile" />
        <p
          v-if="truncated"
          class="m-0 px-2 py-1 text-xs text-gray-400 dark:text-gray-500">
          {{ t('projectFiles.truncated', { shown: nodes.length, total }) }}
        </p>
        <p
          v-if="!nodes.length"
          class="m-0 px-2 py-1 text-xs text-gray-400 dark:text-gray-500">
          {{ t('projectFiles.empty') }}
        </p>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ProjectTreeEntry } from '~/composables/bridge';
import FileTreeNode from './FileTreeNode.vue';
import { logUtil } from '~/utils/log';

/** A tree row: one entry plus its (lazily loaded) children. */
export interface TreeNode extends ProjectTreeEntry {
  /** Path relative to the project root. */
  path: string;
  /** Whether a directory's children were fetched. */
  loaded: boolean;
  expanded: boolean;
  children: TreeNode[];
}

const props = defineProps<{ sessionId: string }>();

const { t } = useI18n();
const projectDirectory = useProjectDirectoryStore();
const rightSidebar = useRightSidebarStore();

const nodes = ref<TreeNode[]>([]);
const loading = ref(false);
const truncated = ref(false);
const total = ref(0);
const error = ref<string | null>(null);
const loadingPaths = ref<Set<string>>(new Set());

const rootLabel = computed(() => {
  const state = projectDirectory.stateFor(props.sessionId);
  return state.directory ?? state.effective ?? '';
});
const unbound = computed(() => !projectDirectory.stateFor(props.sessionId).directory);

function toNodes(parent: string, entries: ProjectTreeEntry[]): TreeNode[] {
  return entries.map(entry => ({
    ...entry,
    path: parent ? `${parent}/${entry.name}` : entry.name,
    loaded: false,
    expanded: false,
    children: []
  }));
}

/** Load the tree root (or reload it after a directory switch). */
async function reload(): Promise<void> {
  if (!props.sessionId || unbound.value) {
    nodes.value = [];
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    const level = await fetchProjectTree(props.sessionId, '');
    nodes.value = toNodes('', level.entries);
    truncated.value = level.truncated;
    total.value = level.total;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    nodes.value = [];
  } finally {
    loading.value = false;
  }
}

/**
 * Expand/collapse a directory, fetching its children on first open.
 * @param node
 */
async function toggleNode(node: TreeNode): Promise<void> {
  if (node.type !== 'dir') return;
  if (node.expanded) {
    node.expanded = false;
    return;
  }
  node.expanded = true;
  if (node.loaded) return;
  loadingPaths.value = new Set(loadingPaths.value).add(node.path);
  try {
    const level = await fetchProjectTree(props.sessionId, node.path);
    node.children = toNodes(node.path, level.entries);
    node.loaded = true;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    const next = new Set(loadingPaths.value);
    next.delete(node.path);
    loadingPaths.value = next;
  }
}

/**
 * Open a file in a right-sidebar tab (one tab per path).
 * @param node
 */
function openFile(node: TreeNode): void {
  if (node.type !== 'file') return;
  rightSidebar.openTab('fileViewer', { path: node.path });
}

/** Re-root whenever the session's project directory changes (turn boundary). */
watch(
  () => [props.sessionId, projectDirectory.stateFor(props.sessionId).directory],
  () => {
    void reload();
  },
  { immediate: true }
);

// Surface a fetch problem once per page mount instead of failing silently.
watch(error, value => {
  if (value) logUtil.w('[ProjectFileTree] load failed:', value);
});
</script>
