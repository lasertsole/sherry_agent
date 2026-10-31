<template>
  <div
    class="relative w-full h-full flex text-theme-main"
    :style="chatBackgroundStyle">
    <!-- Background overlay layer: light mode = white / dark mode = black; opacity is controlled
         by the slider in the "Background Image" tab — the higher it goes, the more the photo is
         washed out toward pure white/pure black until fully obscured. Placed beneath the content
         (pointer-events-none so it does not intercept interactions), and the background image is
         layered over the root container's background, so the content below still sits on top and
         remains selectable. -->
    <div
      v-if="backgroundOpacity > 0"
      class="absolute inset-0 pointer-events-none"
      :style="chatBackgroundOverlayStyle" />
    <!-- Left side - history area (session list sidebar): a standalone component whose
         state/logic has been extracted along with the component.
         The collapsed state is controlled by the parent toolbar button (two-way sync via
         v-model:collapsed); the current session id is two-way synced from the parent via
         v-model:current-session-id (the parent uses it to load the character snapshot). -->
    <SessionSidebar
      v-model:collapsed="isSidebarCollapsed"
      v-model:current-session-id="currentSessionId"
      :extra-width="sideExtra.left" />

    <!-- Right side - session main area -->
    <!-- The conversation keeps TWO floors: 320px absolute (a message row needs
         it) and 35% of the viewport (on a large display it keeps its share
         instead of shrinking to the absolute floor while the side panels eat the
         rest). `max()` applies both; `common/layout.ts` holds the numbers
         because the right column's drag ceiling must agree with them.
         FLOATING: the same node becomes a fixed window inside the shell
         (`fixed` must REPLACE `relative` — same-specificity position utilities);
         the middle column then takes no flex width and the two side columns
         share the freed space (see sideExtra). minWidth must be 0 or the 35%
         floor would win over the computed window width. z-50 puts the window
         ABOVE the right sidebar's maximized overlay (z-40), so it stays visible
         while that panel covers the shell. -->
    <div
      ref="middleColumnRef"
      data-test="chat-column"
      :data-presentation="chatFloat.floating ? 'floating' : 'docked'"
      :class="[
        'flex flex-col h-full',
        chatFloat.floating
          ? [
              'fixed z-50 rounded-xl border border-solid border-gray-light bg-white shadow-2xl dark:border-gray-dark dark:bg-[#181820]',
              draggingFloat || resizingFloat ? 'select-none transition-none' : 'transition-all duration-150'
            ]
          : 'relative flex-1 bg-transparent dark:bg-transparent'
      ]"
      :style="middleColumnStyle">
      <!-- Floating header: the drag handle + the explicit restore button. Only
           the window has it; in the docked layout the toolbar below is the
           top edge. Dragging is ignored when the pointer starts on a control.
           Hidden in the input-only bar (collapsed by dragging the lower bound). -->
      <div
        v-if="chatFloat.floating && !collapsedView"
        class="flex shrink-0 items-center gap-2 border-b border-solid border-gray-light px-3 h-9 dark:border-gray-dark"
        :class="draggingFloat ? 'cursor-grabbing' : 'cursor-grab'"
        data-test="chat-float-header"
        role="button"
        tabindex="0"
        :aria-label="t('chatFloat.dragHint')"
        :title="t('chatFloat.dragHint')"
        @pointerdown="startFloatDrag"
        @pointermove="moveFloatDrag"
        @pointerup="endFloatDrag"
        @pointercancel="cancelFloatDrag">
        <i
          class="pi pi-bars text-xs text-gray-500 dark:text-gray-400"
          aria-hidden="true" />
        <span class="truncate text-xs text-gray-500 dark:text-gray-400">{{ t('chatFloat.title') }}</span>
        <span class="flex-1" />
        <Button
          size="small"
          text
          :label="t('chatFloat.restore')"
          data-test="chat-float-restore"
          @pointerdown.stop
          @click="dockChatFloat" />
      </div>
      <!-- Input-only bar: no header/toolbar, just the composer. The expand
           control appears on hover/focus so the bar stays clean at rest. -->
      <button
        v-if="chatFloat.floating && collapsedView"
        type="button"
        class="absolute -top-6 right-2 z-20 flex h-6 w-6 items-center justify-center rounded-md bg-white/85 text-gray-500 opacity-80 shadow-sm transition-opacity hover:opacity-100 dark:bg-[#181820]/85 dark:text-gray-400"
        data-test="chat-float-expand"
        :title="t('chatFloat.expand')"
        :aria-label="t('chatFloat.expand')"
        @pointerdown.stop
        @click="expandChatFloat">
        <i
          class="pi pi-chevron-up text-xs"
          aria-hidden="true" />
      </button>
      <!-- Resize edges: left/right adjust the width, bottom sets the height and
           snaps into the input-only bar when dragged to the lower bound. Thin
           and translucent so they never cover the composer's controls. -->
      <div
        v-if="chatFloat.floating"
        class="absolute left-0 top-1/2 z-20 h-10 w-1.5 -translate-y-1/2 cursor-ew-resize rounded-full bg-gray-400/30 hover:bg-gray-400/60"
        data-test="chat-float-resize-left"
        role="separator"
        aria-orientation="vertical"
        :aria-label="t('chatFloat.resize')"
        :title="t('chatFloat.resize')"
        @pointerdown="event => startFloatResize(event, 'left')"
        @pointermove="moveFloatResize"
        @pointerup="endFloatResize"
        @pointercancel="cancelFloatResize" />
      <div
        v-if="chatFloat.floating"
        class="absolute right-0 top-1/2 z-20 h-10 w-1.5 -translate-y-1/2 cursor-ew-resize rounded-full bg-gray-400/30 hover:bg-gray-400/60"
        data-test="chat-float-resize-right"
        role="separator"
        aria-orientation="vertical"
        :aria-label="t('chatFloat.resize')"
        :title="t('chatFloat.resize')"
        @pointerdown="event => startFloatResize(event, 'right')"
        @pointermove="moveFloatResize"
        @pointerup="endFloatResize"
        @pointercancel="cancelFloatResize" />
      <div
        v-if="chatFloat.floating"
        class="absolute bottom-0 left-1/2 z-20 h-1.5 w-10 -translate-x-1/2 cursor-ns-resize rounded-full bg-gray-400/30 hover:bg-gray-400/60"
        data-test="chat-float-resize-bottom"
        role="separator"
        aria-orientation="horizontal"
        :aria-label="t('chatFloat.resize')"
        :title="t('chatFloat.resize')"
        @pointerdown="event => startFloatResize(event, 'bottom')"
        @pointermove="moveFloatResize"
        @pointerup="endFloatResize"
        @pointercancel="cancelFloatResize" />
      <!-- Top toolbar: flex with space-between alignment. The collapse/expand history button
           sits on the left; all other function buttons are on the right.
           The button is always visible (after collapsing, the sidebar retracts and this button
           stays in the top-left corner of the session area so it can be expanded again). -->
      <!-- `@container` + the @max variants below: the toolbar's own width is the middle
           column's width, so the row thins out in two steps instead of being squeezed.
           Below 620px the theme switch and the language picker drop out; below 300px only
           the two sidebar collapse buttons remain — the row needs ~250px (both collapse
           buttons plus the folder / menu / toggle trio), so anything under that squeezes the
           row out of the column and a button nobody can reach is worse than a hidden one.
           Container queries measure the CONTENT box, so the 300 excludes the p-3 padding.
           The `!` is load-bearing: PrimeVue's own `display` rules are UNLAYERED, and an
           unlayered rule outranks anything in `@layer utilities`, so a plain
           `@max-[480px]:hidden` would be dead on a `Button` (probe-verified: the same class
           hides a plain element and does nothing to `p-button`). -->
      <div
        v-if="!collapsedView"
        class="@container flex items-center justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- Left: collapse/expand the sidebar, then the body switch
             (sessions ↔ project files) -->
        <div
          class="flex items-center gap-1"
          data-test="toolbar-left">
          <!-- The left column's collapse toggle. Hidden while the chat floats:
               the window owns its restore control, and Ctrl+B still closes it. -->
          <Button
            v-if="!chatFloat.floating"
            :icon="isSidebarCollapsed ? 'pi pi-angle-double-right' : 'pi pi-angle-double-left'"
            :title="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
            :aria-label="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
            variant="text"
            class="text-theme-main"
            @click="toggleSidebar" />
          <!-- Float / restore the conversation column. The button is a toggle:
               docked → pop the middle column out; floating → put it back. -->
          <Button
            v-if="canFloatChat"
            :icon="chatFloat.floating ? 'pi pi-window-minimize' : 'pi pi-window-maximize'"
            :title="chatFloat.floating ? t('toolbar.restoreChat') : t('toolbar.floatChat')"
            :aria-label="chatFloat.floating ? t('toolbar.restoreChat') : t('toolbar.floatChat')"
            :aria-pressed="chatFloat.floating"
            variant="text"
            data-test="chat-float"
            :class="[
              chatFloat.floating
                ? 'bg-theme-main/10 text-theme-main'
                : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'
            ]"
            @click="toggleChatFloat" />
          <Button
            v-if="canShowProjectFiles"
            :icon="showFiles ? 'pi pi-folder-open' : 'pi pi-folder'"
            :title="showFiles ? t('toolbar.sessionList') : t('toolbar.projectFiles')"
            :aria-label="showFiles ? t('toolbar.sessionList') : t('toolbar.projectFiles')"
            :aria-pressed="showFiles"
            variant="text"
            :class="[
              showFiles
                ? 'bg-theme-main/10 text-theme-main'
                : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800',
              '@max-[300px]:hidden!'
            ]"
            @click="toggleSidebarBodyWithHint" />
        </div>
        <!-- Right: the function button area. The theme switch and the language
             picker are NOT here any more — both moved into the left sidebar's
             title row (they are workspace settings, not per-turn controls). -->
        <div
          class="flex items-center gap-3"
          data-test="toolbar-right">
          <div class="hidden md:flex justify-end items-center flex-1 gap-3">
            <!-- Notification entry: 🔔 bell icon + red badge with the unread/merged
                 count. It lives on the RIGHT now — among the tools, where the rest of
                 the shell's entries are; clicking opens the notification TAB (全局)
                 and clears the badge. -->
            <div class="relative flex items-center @max-[300px]:hidden!">
              <Button
                icon="pi pi-bell"
                :title="t('toolbar.notification')"
                :aria-label="t('toolbar.notification')"
                variant="text"
                @click="handleOperate('headerBar', 'notification')" />
              <span
                v-if="notifications.unreadCount > 0"
                class="absolute -top-0.5 -right-0.5 flex min-w-[18px] h-[18px] items-center justify-center rounded-full px-1 text-[10px] leading-none font-medium text-white bg-red-500"
                :title="t('toolbar.notification')">
                {{ notifications.unreadCount > 99 ? '99+' : notifications.unreadCount }}
              </span>
            </div>
            <!-- Session preset (icon-only): opens the preset VIEW as a right-sidebar
                 tab in the 当前会话 group (no popup — the tab survives outside clicks
                 and resizes with the panel). -->
            <SessionPresetButton
              v-if="currentSessionId"
              :session-id="currentSessionId" />
            <!-- Toolbox: the hammer button. Two working surfaces that are not
                 settings — the browser and the user's own terminal — open as
                 tabs in the session group (see toolboxTools). -->
            <Button
              icon="pi pi-hammer"
              :title="t('toolbar.toolbox')"
              :aria-label="t('toolbar.toolbox')"
              variant="text"
              class="@max-[300px]:hidden!"
              data-test="toolbox-button"
              @click="isToolboxOpen = true" />
            <!-- Theme switch: the one app-wide toggle left in the bar (the
                 interface language lives in the 菜单 grid's first entry, where a
                 user who cannot read the current UI will actually find it). It is
                 the first control to go when the column gets narrow — the wrapper
                 is a span because ModeSwitch's root is a fragment. -->
            <span class="@max-[620px]:hidden"><ModeSwitch /></span>
            <!-- Settings menu entry: the three-bars button. All other functions
                 (Skills / Knowledge Graph / System Config / Extend) have been moved from the top
                 bar into the large dialog nine-grid that this button pops open. -->
            <Button
              icon="pi pi-bars"
              :title="t('toolbar.settingsMenu')"
              :aria-label="t('toolbar.settingsMenu')"
              variant="text"
              class="@max-[300px]:hidden!"
              @click="isSettingsMenuOpen = true" />
            <!-- Right sidebar toggle: mirrors the left sidebar's collapse button,
                 sitting at the far right of the toolbar (the sidebar it controls
                 is the rightmost region of the shell). Hidden while the chat
                 floats — the toolbar belongs to the window then, and its side
                 columns are already laid out around it. -->
            <Button
              v-if="!chatFloat.floating"
              :icon="isRightSidebarCollapsed ? 'pi pi-angle-double-left' : 'pi pi-angle-double-right'"
              :title="isRightSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
              :aria-label="isRightSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
              variant="text"
              @click="toggleRightSidebar" />
          </div>
        </div>
      </div>

      <!-- Toolbox: the same dialog shape as the settings menu, with the two
           working surfaces. Two entries share the grid, so each block is wider
           instead of a four-wide row of one. -->
      <Dialog
        v-model:visible="isToolboxOpen"
        :header="t('toolbar.toolbox')"
        :modal="true"
        :closable="true"
        class="w-[min(92vw,560px)]"
        data-test="toolbox-dialog">
        <div class="grid grid-cols-2 gap-4">
          <button
            v-for="tool in toolboxTools"
            :key="tool.event"
            type="button"
            class="flex flex-col items-center justify-center gap-3 w-full h-32 rounded-xl border border-solid border-gray-light dark:border-gray-dark bg-gray-50 dark:bg-gray-800 hover:border-theme-main hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors cursor-pointer"
            :title="t(tool.title ?? tool.toolName)"
            :data-test="`toolbox-${tool.event}`"
            @click="handleToolboxSelect(tool.event)">
            <i :class="[tool.icon, 'text-4xl! text-theme-main']" />
            <span class="text-base text-theme-main">{{ t(tool.toolName) }}</span>
          </button>
        </div>
      </Dialog>

      <!-- Settings menu: shown centered in a large dialog containing the entry grid.
           Each function is a square block with a large icon on top and the function name below.
           Clicking an item directly triggers the corresponding function (right-sidebar tab / route jump). -->
      <Dialog
        v-model:visible="isSettingsMenuOpen"
        :header="t('toolbar.settingsMenu')"
        :modal="true"
        :closable="true"
        class="w-[min(92vw,800px)]">
        <div class="grid grid-cols-4 gap-4">
          <button
            v-for="tool in headerTools"
            :key="tool.event"
            type="button"
            class="flex flex-col items-center justify-center gap-3 w-full h-32 rounded-xl border border-solid border-gray-light dark:border-gray-dark bg-gray-50 dark:bg-gray-800 hover:border-theme-main hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors cursor-pointer"
            :title="t(tool.title ?? tool.toolName)"
            @click="handleMenuSelect(tool.event)">
            <i :class="[tool.icon, 'text-4xl! text-theme-main']" />
            <span class="text-base text-theme-main">{{ t(tool.toolName) }}</span>
          </button>
        </div>
      </Dialog>
      <!-- Session main area: each session is rendered by [sid].vue. Using route.params.sid as
           the page-key gives each session its own KeepAlive cache slot, so switching restores its
           draft/scroll/streaming/HITL state exactly as it was.
           `max` cap: when there are more than N cache slots, KeepAlive evicts the
           least-recently-visited slot by LRU, preventing unbounded growth from deleted inactive
           sessions (their page-key is no longer referenced by the route, but the slot still
           lingers in memory). -->
      <div class="flex flex-1 min-h-0">
        <div class="flex-1 min-w-0 min-h-0">
          <NuxtPage
            :page-key="resolvePageKey"
            :keepalive="{ max: KEEP_ALIVE_MAX }" />
        </div>
      </div>
    </div>

    <!-- Collapsible right sidebar (viewer + tool tabs). It is a full-height column of
         the shell row, exactly like the left session sidebar, so opening it narrows the
         toolbar above the session area instead of sliding in under it — and, living
         outside the KeepAlive'd session page, its tabs survive session switches. -->
    <RightSidebar
      :session-id="openSessionId"
      :extra-width="sideExtra.right"
      @saved="loadCharacter"
      @close-tab="handleCloseRightTab" />

    <!-- Every toolbar / settings-menu entry is a right-sidebar tab (see dialogs.ts), so
         the panels mount and unmount with their tab; the notification badge keeps counting
         because its state lives in the notification STORE, not in the panel. -->

    <!-- Mandatory preset choice for New Chat: a session is created only after a
         preset is picked and applied (persona files + character). Owned by the
         shell so every entry point (left sidebar, chat empty state, toolbar
         command) shares one dialog and one creation path. -->
    <NewSessionPresetDialog v-model:visible="newSession.dialogOpen" />
    <!-- First-run tour. 跳过 hands the user the ordinary preset dialog. -->
    <OnboardingDialog
      v-model:visible="onboardingVisible"
      @skip="newSession.openDialog()" />

    <!-- Ctrl+K command palette: one entry point over the menu grid, the toolbox,
         recent sessions and the skill index. It lives in the shell (not in a
         session page) so the shortcut works everywhere under /home, and its rows
         come from the command registry — never from this template. -->
    <CommandPalette
      v-model:visible="commandPaletteOpen"
      @open-tab="kind => rightSidebarStore.openTab(kind)" />
  </div>
</template>

<script lang="ts" setup>
// Page-level error capture: runtime errors from all descendant components on this page
// (sidebar/toolbar/each dialog, plus child route pages without their own capture)
// → logUtil logging + global toast; returning false stops further upward propagation
// (factory function pattern from 03-errorCapturedFactoryFunction.md)
useErrorCaptured();

// components
import SessionSidebar from './components/SessionSidebar.vue';
import RightSidebar from './components/RightSidebar.vue';
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { detachSideChatSession } from '~/composables/use-side-chat';
import type { RightSidebarTab } from '~/stores/right-sidebar';
import { useSidebarWidthStore } from '~/stores/sidebar-width';
import { useChatFloatStore } from '~/stores/chat-float';
import NewSessionPresetDialog from './components/NewSessionPresetDialog.vue';
import OnboardingDialog from '~/components/OnboardingDialog.vue';
import CommandPalette from '~/components/CommandPalette.vue';
import SessionPresetButton from './components/SessionPresetButton.vue';
import { ensureSessionCharacter } from './components/SessionSidebar.vue';
// function
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { storeToRefs } from 'pinia';
import { useChatBackgroundStore } from '~/stores/chat-background';
import {
  FLOATING_DRAG_CLICK_THRESHOLD_PX,
  FLOATING_MARGIN_PX,
  FLOATING_RESIZE_HEIGHT_MAX_PX,
  FLOATING_RESIZE_HEIGHT_MIN_PX,
  FLOATING_RESIZE_WIDTH_MAX_PX,
  FLOATING_RESIZE_WIDTH_MIN_PX,
  clampCollapsedHeight,
  clampFloatingPosition,
  distributeSideExtra,
  isFloatingAvailable,
  resolveFloatingPosition,
  resolveFloatingSize,
  resolveNearestFloatingAnchor,
  type FloatingPosition,
  type FloatingSize
} from '@/common/floating-window';
import { headerTools, toolboxTools } from './config';
import ModeSwitch from './components/ModeSwitch.vue';
import { buildHomeToolbarCommands, type HomeToolbarContext } from './dialogs';
import { buildNotificationCommandEntries, buildSessionCommandEntries, buildToolbarCommandEntries } from './commands';
import { middleMinWidthCss } from '@/common/layout';

const { t, locale } = useI18n();

/** Global chat area background image: bound to the root container (fills the entire window, including the left session list) */
const chatBackgroundStore = useChatBackgroundStore();
const { backgroundOpacity, chatBackgroundStyle, chatBackgroundOverlayStyle } = storeToRefs(chatBackgroundStore);

/**
 * KeepAlive cache slot cap (LRU).
 *
 * When deleting an **inactive** session: the server-side `clearSession` and the Dexie character
 * snapshot are both cleaned up, but that session's KeepAlive cache slot is not explicitly
 * removed (only when the deleted session is the currently active one does the slot get
 * destroyed along with leaving the `[sid].vue` route via `router.push('/home')`).
 * These leftover slots stay resident in memory and accumulate without bound if uncapped.
 * `max` makes KeepAlive evict the least-recently-visited session by LRU once the slots exceed
 * this number, fundamentally preventing runaway memory growth (does not affect the
 * restore-by-sid semantics; an evicted session is rebuilt on its next visit).
 */
const KEEP_ALIVE_MAX = 20;

/**
 * Compute the KeepAlive page-key for a given route.
 * The standalone tasks page uses its own slot to avoid its KeepAlive state clobbering the chat
 * page's (and vice versa); all other routes (chat page / home) uniformly use the session id as
 * the key.
 * @param route
 * @param route.path
 * @param route.params
 */
const resolvePageKey = (route: { path: string; params: Record<string, unknown> }) => {
  const sid = String(route.params.sid ?? 'root');
  return route.path.includes('/tasks/') ? `tasks-${sid}` : sid;
};

/**
 * Notification state: the badge reads the shared store, whose subscription keeps
 * counting while the notification tab is closed (registered below).
 */
const notifications = useNotificationStore();
/** Web-search store: its socket listener (the Tavily downgrade → notification) is
 *  registered here, once per app shell. */
const webSearchStore = useWebSearchStore();

/**
 * Toolbar command registry: event → command (every entry opens a right-sidebar tab).
 * `handleOperate` is a lookup, so a new toolbar entry only needs a registry row.
 */
const toolbarContext: HomeToolbarContext = {
  openRightTab: (kind, payload) =>
    payload ? rightSidebarStore.openTab(kind, payload) : rightSidebarStore.openTab(kind)
};
const toolbarCommands = buildHomeToolbarCommands(toolbarContext);

/** Global UI store (unified entry for sidebar collapse / settings menu / theme) */
const uiStore = useUiStore();
/** The shell's route: its `sid` param says which session the main area shows. */
const route = useRoute();
/** Whether the sidebar body shows the project file tree. */
const showFiles = computed(() => uiStore.sidebarBody === 'files');

/** The session the main area shows (the route's `sid`). */
const openSessionId = computed(() => String(route.params.sid ?? ''));

/** Per-session project directory (hydrated below; the tree needs a bound root). */
const projectDirectoryStore = useProjectDirectoryStore();

/**
 * Whether the project-files button may appear.
 *
 * The tree it opens is session-scoped AND root-scoped: without an open session, or
 * without a project directory bound to it, there is nothing to show and nobody to
 * show it to — so the button is absent (not merely disabled), and the sidebar body
 * falls back to the session list so a persisted "files" state cannot strand the
 * sidebar on a tree the button can no longer leave.
 */
const canShowProjectFiles = computed(
  () => !!openSessionId.value && !!projectDirectoryStore.stateFor(openSessionId.value).directory
);
watch(
  canShowProjectFiles,
  available => {
    if (!available) uiStore.sidebarBody = 'sessions';
  },
  { immediate: true }
);

// The chip hydrates its own session, but the button must not depend on the chip
// having been opened: pull the binding as soon as the route names a session.
watch(
  openSessionId,
  sid => {
    if (sid) void projectDirectoryStore.hydrate(sid);
  },
  { immediate: true }
);

/** One-time flag: the narrow-screen hint below is shown once per browser. */
const SIDEBAR_FILES_HINT_KEY = 'sherry.sidebarFilesHintShown';

/**
 * Switch the left sidebar's body, hinting once that the sidebar can collapse.
 *
 * Entering files mode usually means opening a file next to it, and at 1280px the
 * fixed chrome (left 280 + right 420) squeezes the chat column hard — the plan's
 * fixed-chrome measurement. The hint costs one toast and is remembered locally.
 */
const toggleSidebarBodyWithHint = (): void => {
  // The working-directory body only exists in the docked column: put the chat
  // back first, otherwise the folder button looks like it did nothing.
  if (chatFloat.floating) dockChatFloat();
  uiStore.toggleSidebarBody();
  // Reveal the body being switched to: with the sidebar collapsed the click would
  // otherwise look like it did nothing (the folder is the most obvious case — you
  // ask for the project files and the tree has to appear).
  uiStore.sidebarCollapsed = false;
  if (uiStore.sidebarBody !== 'files') return;
  try {
    if (window.localStorage.getItem(SIDEBAR_FILES_HINT_KEY)) return;
    window.localStorage.setItem(SIDEBAR_FILES_HINT_KEY, '1');
  } catch {
    // Private mode / storage disabled: show it, the cost is one toast per click.
  }
  toastInfo(t('toolbar.projectFilesHint'), t('toolbar.projectFilesHintDetail'), 6000);
};
/** Whether the settings menu (nine-grid) is open (transient, not persisted) */
const { settingsMenuOpen: isSettingsMenuOpen } = storeToRefs(uiStore);

/** Whether the left history sidebar is collapsed (expanded by default; persisted to localStorage and restored after refresh) */
const { sidebarCollapsed: isSidebarCollapsed } = storeToRefs(uiStore);

/** Right sidebar (viewer + tool tabs): collapse flag + the tab actions the menu uses */
const rightSidebarStore = useRightSidebarStore();
/** Left column width (draggable in SessionSidebar; the auto-collapse reads it) */
const sidebarWidthStore = useSidebarWidthStore();

// ── Floating conversation column ───────────────────────────────────────────
/** Whether the middle column is popped out as a window (persisted, global). */
const chatFloat = useChatFloatStore();
/** Viewport size the float geometry is derived from (updated on window resize). */
const viewportSize = ref({
  width: typeof window === 'undefined' ? 0 : window.innerWidth,
  height: typeof window === 'undefined' ? 0 : window.innerHeight
});
/** The shell's content area: the float stays inside it. */
const floatSlot = computed(() => ({ left: 0, top: 0, ...viewportSize.value }));
/** The middle column element (the composer dock is measured inside it). */
const middleColumnRef = ref<HTMLElement | null>(null);
/** Measured height of the composer dock (0 = no session page / not measured yet). */
const composerDockHeight = ref(0);
let composerDockObserver: ResizeObserver | null = null;
let observedDock: HTMLElement | null = null;

/** The resting size: the user's saved size, or the viewport-ratio default. */
const baseFloatSize = computed(() =>
  resolveFloatingSize(floatSlot.value, { width: chatFloat.width, height: chatFloat.height })
);
/** The input-only bar's height: the composer dock (a fallback while unmeasured). */
const collapsedHeight = computed(() =>
  clampCollapsedHeight(composerDockHeight.value > 0 ? composerDockHeight.value : 160, floatSlot.value)
);
/** True while a downward resize has crossed the expanded minimum. */
const resizingCollapsed = ref(false);
/** The input-only presentation: persisted, or previewed live during a resize. */
const collapsedView = computed(() => chatFloat.floating && (chatFloat.collapsed || resizingCollapsed.value));
/** Live resize preview (null = no resize in progress). */
const resizeDraft = shallowRef<FloatingSize | null>(null);
/** The window's size: the input-only bar, a live resize, or the resting size. */
const floatSize = computed<FloatingSize>(() => {
  const width = resizeDraft.value?.width ?? baseFloatSize.value.width;
  if (collapsedView.value) return { width, height: collapsedHeight.value };
  return resizeDraft.value ?? baseFloatSize.value;
});
/** Whether this window is wide enough to offer the float at all. */
const canFloatChat = computed(() => isFloatingAvailable(viewportSize.value.width));
/** Live drag position (null = resting on the anchor). */
const dragPosition = shallowRef<FloatingPosition | null>(null);
/** Top-left corner kept after a resize (the anchor re-applies on the next move). */
const resizePosition = shallowRef<FloatingPosition | null>(null);
/** The window's current corner: dragging > resizing > the anchor. */
const floatPosition = computed(
  () =>
    dragPosition.value ??
    resizePosition.value ??
    resolveFloatingPosition(chatFloat.anchor, floatSlot.value, floatSize.value)
);

/** (Re)bind the composer-dock observer to the active session page's dock. */
const bindComposerDock = (): void => {
  const dock = middleColumnRef.value?.querySelector<HTMLElement>('[data-test="chat-composer-dock"]') ?? null;
  composerDockHeight.value = dock ? Math.round(dock.getBoundingClientRect().height) : 0;
  if (dock === observedDock) return;
  composerDockObserver?.disconnect();
  composerDockObserver = null;
  observedDock = dock;
  if (!dock || typeof ResizeObserver === 'undefined') return;
  composerDockObserver = new ResizeObserver(() => {
    composerDockHeight.value = Math.round(dock.getBoundingClientRect().height);
  });
  composerDockObserver.observe(dock);
};

/** Stop measuring the dock (the float is docked, or the page is gone). */
const unbindComposerDock = (): void => {
  composerDockObserver?.disconnect();
  composerDockObserver = null;
  observedDock = null;
  composerDockHeight.value = 0;
};
/**
 * The middle column's box. Docked it keeps the conversation's double floor;
 * floating it takes the window's geometry and drops the floor (`minWidth: 0`),
 * or a 35%-viewport minimum would win over the computed width.
 */
const middleColumnStyle = computed(() =>
  chatFloat.floating
    ? {
        left: `${floatPosition.value.left}px`,
        top: `${floatPosition.value.top}px`,
        width: `${floatSize.value.width}px`,
        height: `${floatSize.value.height}px`,
        minWidth: '0px'
      }
    : { minWidth: middleMinWidthCss() }
);
/** True while the header is being dragged (disables the snap transition). */
const draggingFloat = ref(false);
let floatDrag: {
  pointerId: number;
  startX: number;
  startY: number;
  origin: FloatingPosition;
  moved: boolean;
} | null = null;
let pendingDrag: FloatingPosition | null = null;
let dragFrame = 0;

/** Flush the coalesced drag position (one style patch per animation frame). */
const applyPendingDrag = (): void => {
  dragFrame = 0;
  if (!pendingDrag) return;
  dragPosition.value = clampFloatingPosition(pendingDrag, floatSlot.value, floatSize.value);
};

const startFloatDrag = (event: PointerEvent): void => {
  if (event.button !== 0) return;
  // A control inside the header (the restore button) must keep its own click.
  if ((event.target as HTMLElement | null)?.closest('button, a, input, [contenteditable="true"]')) return;
  const header = event.currentTarget as HTMLElement;
  try {
    header.setPointerCapture?.(event.pointerId);
  } catch {
    // Pointer capture is an enhancement (and absent in a bare test DOM).
  }
  floatDrag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    origin: floatPosition.value,
    moved: false
  };
  event.preventDefault();
};

const moveFloatDrag = (event: PointerEvent): void => {
  const session = floatDrag;
  if (!session || session.pointerId !== event.pointerId) return;
  const dx = event.clientX - session.startX;
  const dy = event.clientY - session.startY;
  if (!session.moved && Math.hypot(dx, dy) < FLOATING_DRAG_CLICK_THRESHOLD_PX) return;
  session.moved = true;
  draggingFloat.value = true;
  pendingDrag = { left: session.origin.left + dx, top: session.origin.top + dy };
  if (!dragFrame) dragFrame = requestAnimationFrame(applyPendingDrag);
};

/**
 * End a drag: a sub-threshold pointer sequence is a click; a real drag snaps to
 * the nearest anchor (or, when cancelled, returns to the anchor it started
 * from — the drag position is simply dropped).
 * @param event The pointerup / pointercancel event.
 * @param snap Whether to snap to the nearest anchor (false = abandon the drag).
 */
const finishFloatDrag = (event: PointerEvent, snap: boolean): void => {
  const session = floatDrag;
  if (!session || session.pointerId !== event.pointerId) return;
  floatDrag = null;
  try {
    (event.currentTarget as HTMLElement).releasePointerCapture?.(event.pointerId);
  } catch {
    // The pointer is already gone; nothing to release.
  }
  if (!session.moved) {
    draggingFloat.value = false;
    pendingDrag = null;
    return;
  }
  if (dragFrame) {
    cancelAnimationFrame(dragFrame);
    dragFrame = 0;
  }
  if (pendingDrag) dragPosition.value = clampFloatingPosition(pendingDrag, floatSlot.value, floatSize.value);
  pendingDrag = null;
  draggingFloat.value = false;
  const dropped = dragPosition.value ?? floatPosition.value;
  dragPosition.value = null;
  if (snap) {
    chatFloat.setAnchor(resolveNearestFloatingAnchor(dropped, floatSlot.value, floatSize.value));
    // The anchor now owns the position; a resize offset from before must not win.
    resizePosition.value = null;
  }
};

const endFloatDrag = (event: PointerEvent): void => finishFloatDrag(event, true);
const cancelFloatDrag = (event: PointerEvent): void => finishFloatDrag(event, false);

type FloatResizeMode = 'left' | 'right' | 'bottom';

/** True while an edge is being dragged (disables the snap transition). */
const resizingFloat = ref(false);
let floatResize: {
  pointerId: number;
  mode: FloatResizeMode;
  startX: number;
  startY: number;
  startWidth: number;
  startHeight: number;
  startLeft: number;
  startTop: number;
  moved: boolean;
} | null = null;

/**
 * Start dragging one edge. The opposite edge (and the top) stay put; the
 * position override is captured so a width drag never re-centers the window.
 * @param event The pointerdown event.
 * @param mode Which edge is being dragged.
 */
const startFloatResize = (event: PointerEvent, mode: FloatResizeMode): void => {
  if (event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  const edge = event.currentTarget as HTMLElement;
  try {
    edge.setPointerCapture?.(event.pointerId);
  } catch {
    // Pointer capture is an enhancement (and absent in a bare test DOM).
  }
  resizePosition.value = floatPosition.value;
  floatResize = {
    pointerId: event.pointerId,
    mode,
    startX: event.clientX,
    startY: event.clientY,
    startWidth: floatSize.value.width,
    startHeight: collapsedView.value ? collapsedHeight.value : floatSize.value.height,
    startLeft: floatPosition.value.left,
    startTop: floatPosition.value.top,
    moved: false
  };
  resizingFloat.value = true;
};

const moveFloatResize = (event: PointerEvent): void => {
  const session = floatResize;
  if (!session || session.pointerId !== event.pointerId) return;
  const dx = event.clientX - session.startX;
  const dy = event.clientY - session.startY;
  if (!session.moved && Math.hypot(dx, dy) < FLOATING_DRAG_CLICK_THRESHOLD_PX) return;
  session.moved = true;
  const slot = floatSlot.value;
  if (session.mode === 'bottom') {
    const rawHeight = session.startHeight + dy;
    // Below the expanded minimum the window becomes the input-only bar. Keep
    // the composer (the bar's bottom) where it was, so the input box does not
    // jump when the message area disappears.
    if (composerDockHeight.value > 0 && rawHeight < FLOATING_RESIZE_HEIGHT_MIN_PX) {
      resizingCollapsed.value = true;
      resizePosition.value = {
        left: session.startLeft,
        top: Math.max(0, session.startTop + session.startHeight - collapsedHeight.value)
      };
      resizeDraft.value = { width: session.startWidth, height: collapsedHeight.value };
      return;
    }
    resizingCollapsed.value = false;
    const maxHeight = Math.max(
      FLOATING_RESIZE_HEIGHT_MIN_PX,
      Math.min(FLOATING_RESIZE_HEIGHT_MAX_PX, slot.height - session.startTop - FLOATING_MARGIN_PX)
    );
    resizeDraft.value = {
      width: session.startWidth,
      height: Math.min(maxHeight, Math.max(FLOATING_RESIZE_HEIGHT_MIN_PX, rawHeight))
    };
    return;
  }
  let width: number;
  if (session.mode === 'right') {
    const maxWidth = Math.max(
      FLOATING_RESIZE_WIDTH_MIN_PX,
      Math.min(FLOATING_RESIZE_WIDTH_MAX_PX, slot.width - session.startLeft - FLOATING_MARGIN_PX)
    );
    width = Math.min(maxWidth, Math.max(FLOATING_RESIZE_WIDTH_MIN_PX, session.startWidth + dx));
  } else {
    // Left edge: the right edge stays fixed.
    const rightEdge = session.startLeft + session.startWidth;
    const minLeft = Math.max(FLOATING_MARGIN_PX, rightEdge - FLOATING_RESIZE_WIDTH_MAX_PX);
    const maxLeft = rightEdge - FLOATING_RESIZE_WIDTH_MIN_PX;
    const left = Math.min(maxLeft, Math.max(minLeft, session.startLeft + dx));
    width = rightEdge - left;
    resizePosition.value = { left, top: session.startTop };
  }
  resizeDraft.value = {
    width,
    height: collapsedView.value ? collapsedHeight.value : session.startHeight
  };
};

/**
 * Finish a resize: commit the size (or the input-only state) on release, drop
 * the preview on cancel.
 * @param event The pointerup / pointercancel event.
 * @param commit Whether to persist the draft.
 */
const finishFloatResize = (event: PointerEvent, commit: boolean): void => {
  const session = floatResize;
  if (!session || session.pointerId !== event.pointerId) return;
  floatResize = null;
  try {
    (event.currentTarget as HTMLElement).releasePointerCapture?.(event.pointerId);
  } catch {
    // The pointer is already gone; nothing to release.
  }
  if (!session.moved) {
    resizeDraft.value = null;
    resizingCollapsed.value = false;
    resizingFloat.value = false;
    return;
  }
  const draft = resizeDraft.value;
  if (commit && draft) {
    if (session.mode === 'bottom') {
      if (resizingCollapsed.value) {
        // Keep the last expanded height so the expand control restores it.
        chatFloat.setCollapsed(true);
      } else {
        chatFloat.setCollapsed(false);
        chatFloat.setSize({ width: chatFloat.width, height: draft.height });
      }
    } else {
      chatFloat.setSize({ width: draft.width, height: chatFloat.height });
    }
  }
  resizeDraft.value = null;
  resizingCollapsed.value = false;
  resizingFloat.value = false;
};

const endFloatResize = (event: PointerEvent): void => finishFloatResize(event, true);
const cancelFloatResize = (event: PointerEvent): void => finishFloatResize(event, false);

/** Leave the input-only bar, restoring the stored/default height. */
const expandChatFloat = (): void => {
  // Grow upward: the bar's bottom (the composer) stays put.
  const bottom = floatPosition.value.top + collapsedHeight.value;
  const nextSize = baseFloatSize.value;
  chatFloat.setCollapsed(false);
  resizePosition.value = clampFloatingPosition(
    { left: floatPosition.value.left, top: bottom - nextSize.height },
    floatSlot.value,
    nextSize
  );
};

/**
 * Extra width each side column receives while the chat floats: the docked
 * middle column's width, split equally between the sides that are expanded
 * (only one expanded → it takes everything; none → nothing grows). A maximized
 * right panel covers the shell, so it counts as unexpanded for the split.
 */
const sideExtra = computed(() => {
  if (!chatFloat.floating) return { left: 0, right: 0 };
  const left = uiStore.sidebarCollapsed ? 0 : sidebarWidthStore.width;
  const right = rightSidebarStore.collapsed || rightSidebarStore.maximized ? 0 : rightSidebarStore.width;
  return distributeSideExtra(left, right, viewportSize.value.width);
});

// The This-session tab group belongs to the route's session: switching sessions
// shows that session's own tabs (or an empty group), never the previous one's.
watch(openSessionId, sid => rightSidebarStore.setActiveSession(sid), { immediate: true });

/**
 * Close one right-sidebar tab. Closing a side-chat panel only detaches its
 * socket: the child session, checkpoint and caches survive until the parent
 * session is deleted.
 * @param tab The tab being closed.
 */
const handleCloseRightTab = (tab: RightSidebarTab): void => {
  if (tab.kind === 'sideChat' && tab.payload?.sessionId) {
    detachSideChatSession(tab.payload.sessionId);
  }
  // Keep it in the strip's recent-tabs menu (per-session or global).
  rightSidebarStore.rememberClosed(tab, openSessionId.value);
  rightSidebarStore.closeTab(tab.id);
};
/** Mandatory new-session preset dialog state (shared with every New Chat entry point). */
const newSession = useNewSessionStore();

/**
 * The palette's static half: the menu grid, the toolbox, the notification list
 * and "New chat".
 *
 * Re-registered when the LOCALE changes (not on every render): the labels are
 * resolved i18n text, so an open palette must not keep answering in the old
 * language. Watching `locale` rather than a `watchEffect` is deliberate —
 * `registerStaticBatch` reads and writes the same registry refs, so a tracked
 * effect here would retrigger itself. Re-registration replaces by id.
 */
watch(
  locale,
  () =>
    registerStaticBatch([
      ...buildToolbarCommandEntries(toolbarContext, t),
      ...buildNotificationCommandEntries(toolbarContext, t),
      ...buildSessionCommandEntries(() => newSession.openDialog(), t)
    ]),
  { immediate: true }
);

/** Whether the Ctrl+K palette is open (transient UI state, never persisted). */
const commandPaletteOpen = ref(false);

// The shell owns the global commands the palette / shortcuts drive. The chords
// themselves come from the shortcut table (rebindable in 菜单 → 系统设置), so
// these are plain actions registered by id.
const openCommandPalette = (): void => {
  commandPaletteOpen.value = true;
};
const { collapsed: isRightSidebarCollapsed } = storeToRefs(rightSidebarStore);

/**
 * Callback after system config is saved: the current session keeps its already-locked old
 * snapshot → the display stays unchanged;
 * we only re-read the current session's snapshot to confirm rendering (the latest global values
 * are picked up only when a new session opens).
 * `ensureSessionCharacter` is exported by SessionSidebar.vue for reuse.
 */
const loadCharacter = async () => {
  if (currentSessionId.value) {
    await ensureSessionCharacter(currentSessionId.value);
  }
};

/** Current session id (used for sidebar highlighting + the NuxtPage KeepAlive key) */
const currentSessionId = ref<string>();

/**
 * Tool trigger (header bar only; toolbar/images etc. have moved into [sid].vue along with the session main area)
 * @param type
 * @param event
 */
const handleOperate = (type: string, event: string) => {
  if (!event || type !== 'headerBar') return;
  toolbarCommands[event]?.();
};

/**
 * Settings menu (nine-grid) item click handler: first triggers the corresponding tool event,
 * then collapses the menu. Events reach dialogs and right-sidebar tabs through the same
 * handleOperate dispatch.
 * @param event
 */
/** Toolbox dialog visibility (transient, like the settings menu's flag). */
const isToolboxOpen = ref(false);

/**
 * Run one toolbox entry: close the dialog and dispatch through the same toolbar
 * registry the settings grid uses (so an entry cannot exist without a command).
 * @param event
 */
const handleToolboxSelect = (event: string) => {
  isToolboxOpen.value = false;
  handleOperate('headerBar', event);
};

const handleMenuSelect = (event: string) => {
  isSettingsMenuOpen.value = false;
  handleOperate('headerBar', event);
};

/** Collapse/expand the left history sidebar */
const toggleRightSidebar = () => {
  rightSidebarStore.toggle();
};

const toggleSidebar = () => {
  // While the chat floats the left column is widened by the float's split; the
  // same button now means "hide the sidebar" — close the window and collapse.
  if (chatFloat.floating) {
    dockChatFloat();
    uiStore.sidebarCollapsed = true;
    return;
  }
  isSidebarCollapsed.value = !isSidebarCollapsed.value;
};

// Load the global chat area background image after mount (session list fetching is already
// done inside the SessionSidebar component)
// Narrow-window safety net (ZCode's shell behaviour): when the conversation is
// squeezed below a readable width, the accessory columns retract — right first,
// left second. Only WINDOW resizes trigger it, so opening a panel by hand is
// never undone by the layout. Suspended while the chat floats: the middle
// column takes no flex width then, so measuring would retract the widened sides.
const { measure: measureAutoCollapse } = useAutoCollapse({
  // Read/write the store state directly: `storeToRefs` drops primitive keys on a
  // plain reactive store (and the test double is exactly that), which would
  // leave this accessor reading `undefined`.
  isSidebarCollapsed: () => uiStore.sidebarCollapsed,
  setSidebarCollapsed: value => {
    uiStore.sidebarCollapsed = value;
  },
  sidebarWidth: () => sidebarWidthStore.width,
  isRightCollapsed: () => rightSidebarStore.collapsed,
  setRightCollapsed: value => {
    rightSidebarStore.collapsed = value;
  },
  rightSidebarWidth: () => rightSidebarStore.width,
  isSuspended: () => chatFloat.floating
});

/**
 * Put the conversation column back into the flow. The drag leftovers are
 * cleared and the auto-collapse net re-measures once the restored layout is on
 * screen (its resize path was paused while the window floated).
 */
function dockChatFloat(): void {
  chatFloat.dock();
  // Docking resets the input-only state: the next open is the normal window.
  chatFloat.setCollapsed(false);
  dragPosition.value = null;
  resizePosition.value = null;
  resizeDraft.value = null;
  resizingCollapsed.value = false;
  resizingFloat.value = false;
  pendingDrag = null;
  draggingFloat.value = false;
  unbindComposerDock();
  if (dragFrame) {
    cancelAnimationFrame(dragFrame);
    dragFrame = 0;
  }
  void nextTick(() => {
    requestAnimationFrame(() => measureAutoCollapse());
  });
}

/** Flip between the docked column and the floating window. */
function toggleChatFloat(): void {
  if (chatFloat.floating) {
    dockChatFloat();
    return;
  }
  if (!canFloatChat.value) return;
  chatFloat.open();
}

// The input-only bar needs the composer dock's measured height: bind when the
// window opens, on a session switch, and on a reload that persisted an open
// float (the immediate call runs before mount, so the bind is deferred a frame).
watch(
  () => chatFloat.floating,
  floating => {
    if (!floating) {
      unbindComposerDock();
      return;
    }
    // The page is already mounted when a click opens the float; the rAF retry
    // covers the persisted-open boot and a just-switched session page.
    void nextTick(() => {
      bindComposerDock();
      requestAnimationFrame(bindComposerDock);
    });
  },
  { immediate: true }
);
watch(openSessionId, () => {
  if (!chatFloat.floating) return;
  void nextTick(() => requestAnimationFrame(bindComposerDock));
});

/**
 * The right sidebar's global expand covers the shell, so the conversation has
 * to leave the layout while it is open. Snapshot the states that were true at
 * the moment it expanded (BEFORE auto-floating, or "was docked" would be
 * recorded as "was floating"):
 *  - on expand: remember (left expanded?, chat floating?) and always float;
 *  - on restore: keep floating only when the left was expanded AND the chat was
 *    already floating; every other combination docks.
 * `immediate` covers a boot with a persisted `maximized`: the boot state is the
 * snapshot.
 */
const preMaximizeSnapshot = ref<{ leftExpanded: boolean; middleFloating: boolean } | null>(null);
watch(
  () => rightSidebarStore.maximized,
  maximized => {
    if (maximized) {
      preMaximizeSnapshot.value = {
        leftExpanded: !uiStore.sidebarCollapsed,
        middleFloating: chatFloat.floating
      };
      // A maximize implies an expanded panel. The explicit toggle does this too;
      // the boot path (a persisted `maximized`) does not, and a persisted
      // collapsed flag would leave the overlay with no body — and therefore no
      // restore button — on screen.
      rightSidebarStore.expand?.();
      if (!chatFloat.floating && canFloatChat.value) chatFloat.open();
      return;
    }
    const snapshot = preMaximizeSnapshot.value;
    // `immediate` also runs the restore branch on a normal boot (maximized is
    // false). Only a maximize that actually happened may change the float then —
    // otherwise a persisted floating window would be docked on every reload.
    if (!snapshot) return;
    preMaximizeSnapshot.value = null;
    const keepFloating = !!snapshot.leftExpanded && !!snapshot.middleFloating && canFloatChat.value;
    if (keepFloating) chatFloat.open();
    else dockChatFloat();
  },
  { immediate: true }
);

/** Keep the float's geometry valid when the window changes size. */
const onViewportResize = (): void => {
  viewportSize.value = { width: window.innerWidth, height: window.innerHeight };
  if (chatFloat.floating && !isFloatingAvailable(window.innerWidth)) {
    dockChatFloat();
    return;
  }
  // A resized window keeps its corner; the computed size already re-clamps.
  if (resizePosition.value) {
    resizePosition.value = clampFloatingPosition(resizePosition.value, floatSlot.value, floatSize.value);
  }
};

/**
 * Whether the first-run tour is on screen. Checked ONCE per boot against the
 * persisted `onboarding_completed` flag; the tour itself writes the flag on
 * finish AND on skip, so re-entering the UI never re-opens it.
 */
const onboardingVisible = ref(false);

onMounted(() => {
  chatBackgroundStore.loadBackground();
  // The float's geometry follows the window: its size and anchor are re-derived
  // on resize, and a window too narrow no longer offers the float.
  viewportSize.value = { width: window.innerWidth, height: window.innerHeight };
  window.addEventListener('resize', onViewportResize);
  // Global chords: the palette (Ctrl+K), a new session (Ctrl+N) and the left
  // sidebar (Ctrl+B). Registered by id; the shortcut table decides the keys and
  // a rebind takes effect on the next press.
  registerGlobalHandler('command-palette', openCommandPalette);
  registerGlobalHandler('new-session', () => newSession.openDialog());
  registerGlobalHandler('toggle-sidebar', toggleSidebar);
  // The notification badge must keep counting while its tab is closed, so the
  // listener lives in the store (registered once) and the session socket is
  // established here — the notification TAB is lazily mounted and would
  // otherwise be the first to open it.
  notifications.subscribe();
  // First-run check AFTER the shell is usable: the dialog only needs the flag,
  // and a slow read must not delay the socket subscription above.
  void getAppFlag<boolean>('onboarding_completed').then(done => {
    if (!done) onboardingVisible.value = true;
  });
  // Same for the web-search downgrade frame: it arrives while a TURN runs, so
  // the listener cannot live in the session page (the user may be looking at
  // another session) nor in the Web Search tab (lazily mounted).
  webSearchStore.subscribe();
  useWs();
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', onViewportResize);
  unbindComposerDock();
  unregisterGlobalHandler('command-palette', openCommandPalette);
  unregisterGlobalHandler('new-session');
  unregisterGlobalHandler('toggle-sidebar');
});
</script>
