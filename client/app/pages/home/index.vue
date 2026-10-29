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
      v-model:current-session-id="currentSessionId" />

    <!-- Right side - session main area -->
    <!-- The conversation keeps TWO floors: 320px absolute (a message row needs
         it) and 35% of the viewport (on a large display it keeps its share
         instead of shrinking to the absolute floor while the side panels eat the
         rest). `max()` applies both; `common/layout.ts` holds the numbers
         because the right column's drag ceiling must agree with them. -->
    <div
      class="relative flex flex-col flex-1 h-full bg-transparent dark:bg-transparent"
      :style="{ minWidth: middleMinWidthCss() }">
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
        class="@container flex items-center justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- Left: collapse/expand the sidebar, then the body switch
             (sessions ↔ project files) -->
        <div
          class="flex items-center gap-1"
          data-test="toolbar-left">
          <Button
            :icon="isSidebarCollapsed ? 'pi pi-angle-double-right' : 'pi pi-angle-double-left'"
            :title="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
            :aria-label="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
            variant="text"
            class="text-theme-main"
            @click="toggleSidebar" />
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
                 is the rightmost region of the shell). -->
            <Button
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
import NewSessionPresetDialog from './components/NewSessionPresetDialog.vue';
import CommandPalette from '~/components/CommandPalette.vue';
import SessionPresetButton from './components/SessionPresetButton.vue';
import { ensureSessionCharacter } from './components/SessionSidebar.vue';
// function
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { storeToRefs } from 'pinia';
import { useChatBackgroundStore } from '~/stores/chat-background';
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

/**
 * Ctrl+K (Cmd+K on macOS) opens the palette from anywhere in the shell.
 *
 * The listener is document-level because the shell renders no focusable wrapper;
 * `preventDefault` is unconditional for the chord (the composer does not use
 * Ctrl+K, so there is nothing to protect) and Shift/Alt variants pass through.
 * @param event The keydown.
 */
const onCommandPaletteKeydown = (event: KeyboardEvent): void => {
  if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) return;
  if (event.key !== 'k' && event.key !== 'K') return;
  event.preventDefault();
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
  isSidebarCollapsed.value = !isSidebarCollapsed.value;
};

// Load the global chat area background image after mount (session list fetching is already
// done inside the SessionSidebar component)
// Narrow-window safety net (ZCode's shell behaviour): when the conversation is
// squeezed below a readable width, the accessory columns retract — right first,
// left second. Only WINDOW resizes trigger it, so opening a panel by hand is
// never undone by the layout.
useAutoCollapse({
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
  rightSidebarWidth: () => rightSidebarStore.width
});

onMounted(() => {
  chatBackgroundStore.loadBackground();
  // The Ctrl+K palette's global chord (removed again on unmount; the shell is
  // the only listener, so the login page and dialogs never see it).
  document.addEventListener('keydown', onCommandPaletteKeydown);
  // The notification badge must keep counting while its tab is closed, so the
  // listener lives in the store (registered once) and the session socket is
  // established here — the notification TAB is lazily mounted and would
  // otherwise be the first to open it.
  notifications.subscribe();
  // Same for the web-search downgrade frame: it arrives while a TURN runs, so
  // the listener cannot live in the session page (the user may be looking at
  // another session) nor in the Web Search tab (lazily mounted).
  webSearchStore.subscribe();
  useWs();
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onCommandPaletteKeydown);
});
</script>
