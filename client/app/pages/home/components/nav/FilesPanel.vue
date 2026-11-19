<template>
  <!-- Working-directory body: two collapsible sections (file tree / git graph)
       in ONE shared header row. With both open they stack in a vertical
       Splitter; a single open section renders a plain container (PrimeVue's
       Splitter throws when a v-if panel disappears under a live instance). -->
  <div
    class="flex min-h-0 flex-1 flex-col"
    data-test="files-panel">
    <div class="mb-1 flex shrink-0 items-center gap-1">
      <button
        v-for="section in sidebarSections"
        :key="section.id"
        type="button"
        class="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1.5 py-1 text-xs transition-colors"
        :class="
          section.open ? 'text-gray-600 dark:text-gray-300' : 'text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/60'
        "
        :aria-expanded="section.open"
        :data-test="`sidebar-${section.id}-toggle`"
        @click="section.toggle()">
        <i
          class="pi text-[10px] transition-transform duration-200"
          :class="section.open ? 'pi-chevron-down' : 'pi-chevron-right'" />
        <i :class="[section.icon, 'text-theme-main']" />
        <span class="truncate">{{ t(section.label) }}</span>
      </button>
    </div>

    <Splitter
      v-if="ui.filesSectionOpen && ui.gitSectionOpen"
      layout="vertical"
      class="min-h-0 flex-1"
      data-test="sidebar-splitter"
      @resizeend="onSplitResize">
      <SplitterPanel
        :size="ui.filesSplitSize"
        :min-size="15">
        <div class="flex h-full min-h-0 flex-col">
          <ProjectFileTree
            class="min-h-0 flex-1"
            :session-id="routeSessionId" />
        </div>
      </SplitterPanel>
      <SplitterPanel
        :size="100 - ui.filesSplitSize"
        :min-size="15">
        <div class="flex h-full min-h-0 flex-col">
          <GitGraphPanel
            class="min-h-0 flex-1"
            :session-id="routeSessionId" />
        </div>
      </SplitterPanel>
    </Splitter>
    <div
      v-else-if="ui.filesSectionOpen"
      class="flex min-h-0 flex-1 flex-col"
      data-test="sidebar-files-panel">
      <ProjectFileTree
        class="min-h-0 flex-1"
        :session-id="routeSessionId" />
    </div>
    <div
      v-else-if="ui.gitSectionOpen"
      class="flex min-h-0 flex-1 flex-col"
      data-test="sidebar-git-panel">
      <GitGraphPanel
        class="min-h-0 flex-1"
        :session-id="routeSessionId" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
// useUiStore is a Nuxt auto-import; keep it implicit so integration suites can
// stub the global (the original SessionSidebar did the same).
import ProjectFileTree from '../ProjectFileTree.vue';
import GitGraphPanel from '../GitGraphPanel.vue';

const { t } = useI18n();
const ui = useUiStore();
const route = useRoute();

/** The two working-directory sections: their headers share one row. */
const sidebarSections = computed<Array<{ id: string; icon: string; label: string; open: boolean; toggle: () => void }>>(
  () => [
    {
      id: 'files',
      icon: 'pi pi-folder',
      label: 'projectFiles.title',
      open: ui.filesSectionOpen,
      toggle: () => ui.toggleFilesSection()
    },
    {
      id: 'git',
      icon: 'pi pi-sitemap',
      label: 'gitGraph.title',
      open: ui.gitSectionOpen,
      toggle: () => ui.toggleGitSection()
    }
  ]
);

/** The session in view — the tree belongs to that session's project directory. */
const routeSessionId = computed(() => String(route.params.sid ?? ''));

/**
 * Remember the split the gutter was dragged to (PrimeVue reports the panel sizes).
 * @param event Splitter resizeend event carrying panel shares in per cent.
 * @param event.sizes
 */
const onSplitResize = (event: { sizes: number[] }): void => {
  if (ui.filesSectionOpen && ui.gitSectionOpen && Array.isArray(event.sizes) && event.sizes[0] !== undefined) {
    ui.setFilesSplitSize(event.sizes[0]);
  }
};
</script>
