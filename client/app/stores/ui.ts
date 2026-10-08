import { defineStore } from 'pinia';

/**
 * Global UI state store: unified entry point for sidebar collapse, settings menu toggle, theme switching,
 * and the settings-menu toggle.
 * - sidebarCollapsed: persisted to localStorage (specified via pick), restored after refresh/app restart
 * - settingsMenuOpen: transient popup toggle, deliberately not persisted
 * - Theme persistence is handled by @nuxtjs/color-mode (cookie); this store only provides the unified write entry
 */
export const useUiStore = defineStore(
  'ui',
  () => {
    // Note: useColorMode depends on the Nuxt setup context and must be captured inside the store
    // setup function body (the first useUiStore() call happens in a component's setup; the closure
    // reference remains valid afterwards)
    const colorMode = useColorMode();
    const sidebarCollapsed = ref(false);
    /** What the left sidebar shows: the session list or the project file tree. */
    const sidebarBody = ref<'sessions' | 'files'>('sessions');
    const settingsMenuOpen = ref(false);
    /** The 工作目录 body's two sections (file tree / git graph), each collapsible. */
    const filesSectionOpen = ref(true);
    const gitSectionOpen = ref(true);
    /** Share of the body's height the file-tree section keeps (the draggable split). */
    const filesSplitSize = ref(50);
    const setTheme = (value: string) => {
      colorMode.preference = value;
    };
    const toggleSidebar = () => {
      sidebarCollapsed.value = !sidebarCollapsed.value;
    };
    const toggleSidebarBody = () => {
      sidebarBody.value = sidebarBody.value === 'sessions' ? 'files' : 'sessions';
    };
    const toggleFilesSection = () => {
      filesSectionOpen.value = !filesSectionOpen.value;
    };
    const toggleGitSection = () => {
      gitSectionOpen.value = !gitSectionOpen.value;
    };
    /**
     * Remember the split the user dragged (clamped so neither section vanishes).
     * @param size New share of the file-tree section, in per cent.
     */
    const setFilesSplitSize = (size: number) => {
      filesSplitSize.value = Math.min(85, Math.max(15, Math.round(size)));
    };
    return {
      sidebarCollapsed,
      settingsMenuOpen,
      sidebarBody,
      filesSectionOpen,
      gitSectionOpen,
      filesSplitSize,
      setTheme,
      toggleSidebar,
      toggleSidebarBody,
      toggleFilesSection,
      toggleGitSection,
      setFilesSplitSize
    };
  },
  {
    persist: {
      pick: ['sidebarCollapsed', 'sidebarBody', 'filesSectionOpen', 'gitSectionOpen', 'filesSplitSize']
    }
  }
);
