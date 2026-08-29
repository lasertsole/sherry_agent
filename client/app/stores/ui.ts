import { defineStore } from 'pinia';

/**
 * UI 状态全局 store：侧边栏折叠、设置菜单开关、主题切换统一入口。
 * - sidebarCollapsed: 持久化到 localStorage（pick 指定），刷新/重开应用后恢复
 * - settingsMenuOpen: 瞬态弹层开关，刻意不持久化
 * - 主题持久化由 @nuxtjs/color-mode（cookie）承担，本 store 仅提供统一写入口
 */
export const useUiStore = defineStore(
  'ui',
  () => {
    // 注意：useColorMode 依赖 Nuxt setup 上下文，必须在 store setup 函数体内捕获
    // （首次 useUiStore() 调用发生在组件 setup 中，此后闭包引用一直有效）
    const colorMode = useColorMode();
    const sidebarCollapsed = ref(false);
    const settingsMenuOpen = ref(false);
    const setTheme = (value: string) => {
      colorMode.preference = value;
    };
    const toggleSidebar = () => {
      sidebarCollapsed.value = !sidebarCollapsed.value;
    };
    return { sidebarCollapsed, settingsMenuOpen, setTheme, toggleSidebar };
  },
  {
    persist: {
      pick: ['sidebarCollapsed']
    }
  }
);
