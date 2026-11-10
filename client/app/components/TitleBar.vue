<template>
  <!-- Custom title bar (desktop only).
       The Tauri window runs with `decorations: false`, so this bar is the only
       drag handle and the only set of window controls. It spans the conversation
       and the right sidebar normally, and the full window when the left column is
       collapsed or the right panel is maximized. Fixed + z-60 keeps it above the
       right panel's maximized overlay (z-40) and the floating conversation (z-50).
       The Web build keeps the browser's own chrome and draws nothing. -->
  <div
    v-if="visible"
    class="titlebar fixed top-0 right-0 z-[60] flex select-none items-center border-b border-solid border-gray-light bg-white dark:border-gray-dark dark:bg-[#181820]"
    data-test="titlebar"
    :data-maximized="isMaximized ? 'true' : 'false'"
    :style="{ left: `${leftOffset}px`, height: TITLEBAR_HEIGHT_CSS }">
    <!-- macOS convention: the window controls sit on the left. -->
    <div
      v-if="isMac"
      class="flex h-full shrink-0 items-center"
      data-test="titlebar-controls">
      <button
        v-for="btn in controlButtons"
        :key="btn.action"
        type="button"
        class="titlebar-btn"
        :class="{ 'titlebar-btn--close': btn.action === 'close' }"
        :title="btn.title"
        :aria-label="btn.title"
        :data-test="btn.testId"
        @pointerdown.stop
        @click="btn.handler">
        <i :class="btn.icon" />
      </button>
    </div>

    <!-- Drag region: the empty area between the controls; Tauri starts a window
         drag (and a double-click maximize) when the pointer goes down here. -->
    <div
      class="flex h-full min-w-0 flex-1 items-center px-3"
      data-tauri-drag-region
      data-test="titlebar-drag">
      <span class="truncate text-xs text-gray-500 dark:text-gray-400">{{ appTitle }}</span>
    </div>

    <!-- Windows / Linux convention: the window controls sit on the right. -->
    <div
      v-if="!isMac"
      class="flex h-full shrink-0 items-center"
      data-test="titlebar-controls">
      <button
        v-for="btn in controlButtons"
        :key="btn.action"
        type="button"
        class="titlebar-btn"
        :class="{ 'titlebar-btn--close': btn.action === 'close' }"
        :title="btn.title"
        :aria-label="btn.title"
        :data-test="btn.testId"
        @pointerdown.stop
        @click="btn.handler">
        <i :class="btn.icon" />
      </button>
    </div>
  </div>
</template>

<script lang="ts" setup>
/**
 * Custom window title bar.
 *
 * @module components/TitleBar
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { isTauriRuntime } from '~/utils/project-directory';
import { TITLEBAR_HEIGHT_CSS } from '~/common/titlebar';
import { logUtil } from '~/utils/log';

/** One window-control button in the bar. */
interface TitlebarButton {
  action: 'minimize' | 'toggleMaximize' | 'close';
  icon: string;
  title: string;
  testId: string;
  handler: () => void;
}

const { t } = useI18n();
const route = useRoute();

/** The shell draws its own bar only over the desktop window. */
const isDesktop = isTauriRuntime();
const visible = isDesktop;

/** macOS keeps its controls on the left, in the traffic-light order. */
const isMac = typeof navigator !== 'undefined' && /mac/i.test(navigator.userAgent);

const uiStore = useUiStore();
const rightSidebarStore = useRightSidebarStore();
const sidebarWidthStore = useSidebarWidthStore();

const appTitle = computed(() => t('chatBox.defaultAiName'));

/** Whether the window currently fills the screen (drives the maximize label). */
const isMaximized = ref(false);

/**
 * The bar's left edge: the left column's width only where the left column is a
 * full-height column of the home shell. Everywhere else (login, a collapsed left
 * column, the right panel's full-window overlay) the bar spans the window.
 */
const leftOffset = computed(() => {
  if (!route.path.startsWith('/home') || uiStore.sidebarCollapsed || rightSidebarStore.maximized) return 0;
  return sidebarWidthStore.width;
});

/**
 * Run one window-control action through the Tauri window API.
 *
 * The close button deliberately calls `close()` instead of hiding or quitting:
 * that is the same CloseRequested event the OS close button produced, so the
 * Rust shell's interception and the existing `CloseToTrayDialog` preference
 * flow answer it — there is no second close path.
 * @param action Which control was pressed.
 */
async function runWindowAction(action: TitlebarButton['action']): Promise<void> {
  if (!isDesktop) return;
  try {
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    const win = getCurrentWindow();
    if (action === 'minimize') {
      await win.minimize();
      return;
    }
    if (action === 'toggleMaximize') {
      await win.toggleMaximize();
      isMaximized.value = await win.isMaximized();
      return;
    }
    // Close: the Rust shell intercepts this close request, asks the frontend
    // (CloseToTrayDialog) and only exits when that flow chooses "quit".
    await win.close();
  } catch (e) {
    // A failed window action must not break the page; the OS controls are gone,
    // so the next click simply tries again.
    logUtil.e('[TitleBar] window action failed:', e);
  }
}

/** Re-read the maximized state (the window's own resize event reports it). */
async function syncMaximized(): Promise<void> {
  if (!isDesktop) return;
  try {
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    isMaximized.value = await getCurrentWindow().isMaximized();
  } catch {
    // Not fatal: the button keeps the last known state.
  }
}

const controlButtons = computed<TitlebarButton[]>(() => {
  const minimize: TitlebarButton = {
    action: 'minimize',
    icon: 'pi pi-minus',
    title: t('titlebar.minimize'),
    testId: 'titlebar-minimize',
    handler: () => void runWindowAction('minimize')
  };
  const maximize: TitlebarButton = {
    action: 'toggleMaximize',
    icon: isMaximized.value ? 'pi pi-window-minimize' : 'pi pi-window-maximize',
    title: t(isMaximized.value ? 'titlebar.restore' : 'titlebar.maximize'),
    testId: 'titlebar-maximize',
    handler: () => void runWindowAction('toggleMaximize')
  };
  const close: TitlebarButton = {
    action: 'close',
    icon: 'pi pi-times',
    title: t('titlebar.close'),
    testId: 'titlebar-close',
    handler: () => void runWindowAction('close')
  };
  // macOS: close, minimize, maximize from the left; elsewhere: minimize,
  // maximize, close from the right (the classic Windows order).
  return isMac ? [close, minimize, maximize] : [minimize, maximize, close];
});

onMounted(() => {
  if (!isDesktop) return;
  void syncMaximized();
  // Maximizing via the drag region's double-click never goes through a control,
  // so the DOM resize event is what keeps the label in step.
  window.addEventListener('resize', syncMaximized);
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', syncMaximized);
});
</script>

<style lang="scss" scoped>
.titlebar {
  // Matches the side columns' 300ms collapse animation, so the left edge slides
  // with the sidebar instead of jumping.
  transition: left 0.3s ease;
}

.titlebar-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 100%;
  padding: 0;
  font-size: 10px;
  color: #6b7280;
  background: transparent;
  border: none;
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;

  &:hover {
    background-color: rgba(0, 0, 0, 0.08);
  }
}

// Windows convention: the close button turns red on hover.
.titlebar-btn--close:hover {
  background-color: #e81123;
  color: #ffffff;
}

:global(.dark) .titlebar-btn {
  color: #9ca3af;

  &:hover {
    background-color: rgba(255, 255, 255, 0.08);
  }
}

:global(.dark) .titlebar-btn--close:hover {
  background-color: #e81123;
  color: #ffffff;
}
</style>
