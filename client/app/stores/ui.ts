import { defineStore } from 'pinia';

/**
 * Global UI state store: unified entry point for sidebar collapse, settings menu toggle, theme switching,
 * and the plan (todo) dock collapse state.
 * - sidebarCollapsed / todoDockCollapsed: persisted to localStorage (specified via pick), restored after refresh/app restart
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
    const todoDockCollapsed = ref(false);
    /** The 工作目录 body's two sections (file tree / git graph), each collapsible. */
    const filesSectionOpen = ref(true);
    const gitSectionOpen = ref(true);
    const setTheme = (value: string) => {
      colorMode.preference = value;
    };
    const toggleSidebar = () => {
      sidebarCollapsed.value = !sidebarCollapsed.value;
    };
    const toggleTodoDock = () => {
      todoDockCollapsed.value = !todoDockCollapsed.value;
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
    return {
      sidebarCollapsed,
      settingsMenuOpen,
      todoDockCollapsed,
      sidebarBody,
      filesSectionOpen,
      gitSectionOpen,
      setTheme,
      toggleSidebar,
      toggleTodoDock,
      toggleSidebarBody,
      toggleFilesSection,
      toggleGitSection
    };
  },
  {
    persist: {
      pick: ['sidebarCollapsed', 'todoDockCollapsed', 'sidebarBody', 'filesSectionOpen', 'gitSectionOpen']
    }
  }
);
