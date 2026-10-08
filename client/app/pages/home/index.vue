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
    <div class="relative flex flex-col flex-1 min-w-0 h-full bg-transparent dark:bg-transparent">
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
            <!-- Theme switch + language picker: the two app-wide switches, sitting
                 immediately LEFT of the menu button. They are the first controls to
                 go when the column gets narrow (the theme wrapper is a span because
                 ModeSwitch's root is a fragment; the picker carries the important
                 modifier because PrimeVue sets its own `display`). -->
            <span class="@max-[620px]:hidden"><ModeSwitch /></span>
            <Select
              v-model="locale"
              :options="languageOptions"
              option-label="name"
              option-value="code"
              class="@max-[620px]:hidden! w-40"
              size="small"
              :aria-label="t('a11y.language')"
              data-test="toolbar-locale"
              @update:model-value="onLanguageChange">
              <template #value="slotProps">
                <span
                  v-if="slotProps.value"
                  class="flex items-center gap-1.5">
                  <i class="pi pi-globe" />
                  <span>{{ t(`config.language.${slotProps.value}`) }}</span>
                </span>
                <span
                  v-else
                  class="flex items-center gap-1.5">
                  <i class="pi pi-globe" />
                  <span>{{ t('config.language.zh') }}</span>
                </span>
              </template>
              <template #option="slotProps">
                <span>{{ t(`config.language.${slotProps.option.code}`) }}</span>
              </template>
            </Select>
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
    <RightSidebar @saved="loadCharacter" />

    <!-- Every toolbar / settings-menu entry is a right-sidebar tab (see dialogs.ts), so
         the panels mount and unmount with their tab; the notification badge keeps counting
         because its state lives in the notification STORE, not in the panel. -->

    <!-- Mandatory preset choice for 新建对话: a session is created only after a
         preset is picked and applied (persona files + character). Owned by the
         shell so every entry point (left sidebar, chat empty state, toolbar
         command) shares one dialog and one creation path. -->
    <NewSessionPresetDialog v-model:visible="newSession.dialogOpen" />
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
import NewSessionPresetDialog from './components/NewSessionPresetDialog.vue';
import SessionPresetButton from './components/SessionPresetButton.vue';
import { ensureSessionCharacter } from './components/SessionSidebar.vue';
// function
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { storeToRefs } from 'pinia';
import { useChatBackgroundStore } from '~/stores/chat-background';
import { headerTools, toolboxTools } from './config';
import ModeSwitch from './components/ModeSwitch.vue';
import { buildHomeToolbarCommands } from './dialogs';

const { t, locale, setLocale } = useI18n();

/** Language switcher options: the names come from System Config's language block. */
const languageOptions = computed(() => [
  { name: t('config.language.zh'), code: 'zh' },
  { name: t('config.language.en'), code: 'en' },
  { name: t('config.language.ja'), code: 'ja' },
  { name: t('config.language.ko'), code: 'ko' }
]);

/**
 * Language switch handler: switches via nuxt-i18n's `setLocale` (under the
 * `no_prefix` strategy its internal `navigate()` returns early, so `/home/:id`
 * URLs stay stable) and then writes the preference cookie — the module's own
 * cookie write is a no-op with `detectBrowserLanguage: false`, and the cookie is
 * what makes the choice survive a refresh (app.vue reads the same key).
 * @param code
 */
async function onLanguageChange(code: string) {
  await setLocale(code as 'zh' | 'en' | 'ja' | 'ko');
  persistLocalePreference(code as 'zh' | 'en' | 'ja' | 'ko');
}

/**
 * Persist the language preference cookie (key: `i18n_redirected`).
 * @param code
 */
function persistLocalePreference(code: 'zh' | 'en' | 'ja' | 'ko') {
  if (import.meta.server) return;
  const pref = useCookie('i18n_redirected');
  pref.value = code;
}

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

/**
 * Toolbar command registry: event → command (every entry opens a right-sidebar tab).
 * `handleOperate` is a lookup, so a new toolbar entry only needs a registry row.
 */
const toolbarCommands = buildHomeToolbarCommands({
  openRightTab: (kind, payload) =>
    payload ? rightSidebarStore.openTab(kind, payload) : rightSidebarStore.openTab(kind)
});

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
/** Mandatory new-session preset dialog state (shared with every 新建对话 entry point). */
const newSession = useNewSessionStore();
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
onMounted(() => {
  chatBackgroundStore.loadBackground();
  // The notification badge must keep counting while its tab is closed, so the
  // listener lives in the store (registered once) and the session socket is
  // established here — the notification TAB is lazily mounted and would
  // otherwise be the first to open it.
  notifications.subscribe();
  useWs();
});
</script>

<i18n lang="json">
{
  "en": {
    "toolbar": {
      "collapseSidebar": "Collapse sidebar",
      "expandSidebar": "Expand sidebar",
      "notification": "Notifications",
      "projectFiles": "Project files",
      "projectFilesHint": "Project files",
      "projectFilesHintDetail": "Tip: collapse the left sidebar to give the file tree and the chat more room.",
      "sessionList": "Sessions",
      "settingsMenu": "Menu",
      "toolbox": "Toolbox",
      "browser": "Browser",
      "terminal": "Terminal"
    }
  },
  "zh": {
    "toolbar": {
      "collapseSidebar": "折叠侧边栏",
      "expandSidebar": "展开侧边栏",
      "notification": "通知",
      "projectFiles": "项目文件",
      "projectFilesHint": "项目文件",
      "projectFilesHintDetail": "提示：可折叠左侧栏，给文件树和对话区让出更多空间。",
      "sessionList": "会话列表",
      "settingsMenu": "菜单",
      "toolbox": "工具箱",
      "browser": "浏览器",
      "terminal": "终端"
    }
  },
  "ja": {
    "toolbar": {
      "collapseSidebar": "サイドバーを折りたたむ",
      "expandSidebar": "サイドバーを展開",
      "notification": "通知",
      "projectFiles": "プロジェクトファイル",
      "projectFilesHint": "プロジェクトファイル",
      "projectFilesHintDetail": "ヒント：左サイドバーを折りたたむと、ファイルツリーとチャットに余裕ができます。",
      "sessionList": "セッション一覧",
      "settingsMenu": "メニュー",
      "toolbox": "ツールボックス",
      "browser": "ブラウザ",
      "terminal": "ターミナル"
    }
  },
  "ko": {
    "toolbar": {
      "collapseSidebar": "사이드바 접기",
      "expandSidebar": "사이드바 펼치기",
      "notification": "알림",
      "projectFiles": "프로젝트 파일",
      "projectFilesHint": "프로젝트 파일",
      "projectFilesHintDetail": "팁: 왼쪽 사이드바를 접으면 파일 트리와 채팅 공간이 넓어집니다.",
      "sessionList": "세션 목록",
      "settingsMenu": "메뉴",
      "toolbox": "도구 상자",
      "browser": "브라우저",
      "terminal": "터미널"
    }
  }
}
</i18n>
