<template>
  <div>
    <button
      type="button"
      class="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-800"
      :style="{ paddingLeft: `${depth * 12 + 8}px` }"
      :title="node.path"
      :data-test="`file-node-${node.path}`"
      @click="onClick">
      <i
        v-if="node.type === 'dir'"
        class="pi pi-angle-right text-[0.6rem] text-gray-400 transition-transform"
        :class="{ 'rotate-180': node.expanded }"></i>
      <i
        v-else
        class="pi pi-file text-gray-400"></i>
      <i
        v-if="node.type === 'dir'"
        class="pi"
        :class="node.expanded ? 'pi-folder-open text-theme-main' : 'pi-folder text-theme-main'"></i>
      <span class="truncate">{{ node.name }}</span>
      <ProgressSpinner
        v-if="loadingPaths.has(node.path)"
        style="width: 0.75rem; height: 0.75rem"
        class="ml-auto" />
    </button>

    <template v-if="node.expanded">
      <FileTreeNode
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
        :session-id="sessionId"
        :loading-paths="loadingPaths"
        @toggle="child => emit('toggle', child)"
        @open="child => emit('open', child)" />
    </template>
  </div>
</template>

<script setup lang="ts">
import type { TreeNode } from './ProjectFileTree.vue';

const props = defineProps<{
  node: TreeNode;
  depth: number;
  sessionId: string;
  loadingPaths: Set<string>;
}>();

const emit = defineEmits<{
  (e: 'toggle', node: TreeNode): void;
  (e: 'open', node: TreeNode): void;
}>();

/** Directories expand/collapse; files open in a viewer tab. */
const onClick = (): void => {
  if (props.node.type === 'dir') emit('toggle', props.node);
  else emit('open', props.node);
};
</script>
