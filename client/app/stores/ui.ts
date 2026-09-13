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
    const settingsMenuOpen = ref(false);
    const todoDockCollapsed = ref(false);
    const setTheme = (value: string) => {
      colorMode.preference = value;
    };
    const toggleSidebar = () => {
      sidebarCollapsed.value = !sidebarCollapsed.value;
    };
    const toggleTodoDock = () => {
      todoDockCollapsed.value = !todoDockCollapsed.value;
    };
    return { sidebarCollapsed, settingsMenuOpen, todoDockCollapsed, setTheme, toggleSidebar, toggleTodoDock };
  },
  {
    persist: {
      pick: ['sidebarCollapsed', 'todoDockCollapsed']
    }
  }
);
