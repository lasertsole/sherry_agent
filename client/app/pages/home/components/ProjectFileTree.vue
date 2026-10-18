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
      <div
        v-if="loading && !nodes.length"
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
          @open="openFile"
          @menu="openRowMenu" />
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

    <!-- One menu for the whole tree: rows emit right-clicks upward (a per-row
         popup would mount one hidden ContextMenu per file). -->
    <ContextMenu
      ref="rowMenuRef"
      :model="rowMenuItems"
      data-test="file-tree-context-menu" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ProjectTreeEntry } from '~/composables/bridge';
import type { FileTreeMenuItem, FileTreeMenuNode } from '~/composables/use-file-tree-menu';
import FileTreeNode from './FileTreeNode.vue';
import { logUtil } from '~/utils/log';
import {
  canUseDesktopFileActions,
  desktopPlatform,
  openPathWithSystemApp,
  revealPathInFileManager
} from '~/utils/desktop-file-actions';

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
  if (!props.sessionId) {
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

/** Items the row ContextMenu renders (rebuilt per right-click). */
const rowMenuItems = ref<FileTreeMenuItem[]>([]);
const rowMenuRef = useTemplateRef<{ show: (event: Event) => void }>('rowMenuRef');

/**
 * The "Reveal in file manager" label follows the platform, like ZCode's row
 * menu does (Finder / Explorer / file manager).
 */
const revealLabel = computed(() => {
  const platform = desktopPlatform();
  if (platform === 'macos') return t('projectFiles.revealMac');
  if (platform === 'windows') return t('projectFiles.revealWindows');
  return t('projectFiles.reveal');
});

/**
 * Copy a string and report the outcome (the clipboard helper never throws).
 * @param text
 */
async function copyAndToast(text: string): Promise<void> {
  const ok = await copyTextToClipboard(text);
  if (ok) toastSuccess(t('projectFiles.copied'));
  else toastError(t('projectFiles.copyFailed'));
}

/**
 * Open an HTML file in the built-in browser panel.
 *
 * A frame cannot load `file://` (and the desktop webview's CSP refuses frames
 * altogether), so the page is served from the read-only file endpoint and
 * handed to the iframe as a blob URL. Relative references inside the page
 * (CSS/JS) therefore do not resolve — the panel's blank state names the
 * limitation, and "Open with the system application" is the full-fidelity
 * alternative on the desktop build.
 * @param node
 */
async function openInBuiltInBrowser(node: TreeNode): Promise<void> {
  try {
    const file = await fetchProjectFile(props.sessionId, node.path);
    const url = URL.createObjectURL(new Blob([file.content], { type: 'text/html' }));
    rightSidebar.openTab('browser', { instance: `file#${Date.now()}`, url });
  } catch (e) {
    toastError(t('projectFiles.openInBrowserFailed'), e instanceof Error ? e.message : String(e));
  }
}

/**
 * Show the row's context menu (one ContextMenu for the whole tree).
 * @param event The row's contextmenu event (anchors the popup).
 * @param node The right-clicked row.
 */
function openRowMenu(event: Event, node: TreeNode): void {
  const row: FileTreeMenuNode = { type: node.type, path: node.path, name: node.name };
  rowMenuItems.value = buildFileTreeMenu({
    node: row,
    sessionId: props.sessionId,
    rootPath: rootLabel.value,
    desktop: canUseDesktopFileActions(),
    labels: {
      open: t('projectFiles.open'),
      openWith: t('projectFiles.openWith'),
      openInViewer: t('projectFiles.openInViewer'),
      openWithSystem: t('projectFiles.openWithSystem'),
      openInBrowser: t('projectFiles.openInBrowser'),
      reveal: revealLabel.value,
      copyAbsolutePath: t('projectFiles.copyAbsolutePath'),
      copyRelativePath: t('projectFiles.copyRelativePath'),
      addToChat: t('projectFiles.addToChat')
    },
    open: () => {
      if (node.type === 'dir') void toggleNode(node);
      else openFile(node);
    },
    openSystem: () => {
      void openPathWithSystemApp(absolutePathOf(rootLabel.value, node.path));
    },
    openBrowser: () => {
      void openInBuiltInBrowser(node);
    },
    reveal: () => {
      void revealPathInFileManager(absolutePathOf(rootLabel.value, node.path)).then(ok => {
        if (!ok) toastError(t('projectFiles.revealFailed'));
      });
    },
    copy: text => void copyAndToast(text),
    addToChat: () => {
      requestFileMention(props.sessionId, mentionPayloadOf(row));
    }
  });
  rowMenuRef.value?.show(event);
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
