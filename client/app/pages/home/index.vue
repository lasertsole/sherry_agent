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
      <!-- `@container` + the @max-[620px] variants below: the toolbar's own width is the
           middle column's width, so when the two sidebars squeeze it the theme switch and
           the language picker drop out instead of wrapping the button row. -->
      <div
        class="@container flex items-center justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- Left: collapse/expand the history sidebar -->
        <Button
          :icon="isSidebarCollapsed ? 'pi pi-angle-double-right' : 'pi pi-angle-double-left'"
          :title="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
          :aria-label="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
          variant="text"
          class="text-theme-main"
          @click="toggleSidebar" />
        <!-- Right: original function button area (the theme switch and the language
             picker are the first to go when the column gets narrow) -->
        <div class="flex items-center gap-3">
          <span class="@max-[620px]:hidden"><ModeSwitch /></span>
          <div class="hidden md:flex justify-end items-center flex-1 gap-3">
            <!-- Language switcher: moved from System Config > Language Settings to the top
                 toolbar; reads/writes the vue-i18n locale directly.
                 The globe icon (pi-globe) lets users of any language intuitively recognize
                 this as the language switch control. -->
            <Select
              :model-value="locale"
              :options="languageOptions"
              option-label="name"
              option-value="code"
              class="@max-[620px]:hidden! w-40"
              size="small"
              :aria-label="t('a11y.language')"
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
            <!-- Notification entry: 🔔 bell icon + red badge with the unread/merged count.
                 Clicking opens the notification dialog and clears the unread count. -->
            <div class="relative flex items-center">
              <Button
                icon="pi pi-bell"
                :title="t('toolbar.notification')"
                :aria-label="t('toolbar.notification')"
                variant="text"
                @click="handleOperate('headerBar', 'notification')" />
              <span
                v-if="notificationUnread > 0"
                class="absolute -top-0.5 -right-0.5 flex min-w-[18px] h-[18px] items-center justify-center rounded-full px-1 text-[10px] leading-none font-medium text-white bg-red-500"
                :title="t('toolbar.notification')">
                {{ notificationUnread > 99 ? '99+' : notificationUnread }}
              </span>
            </div>
            <!-- Settings menu entry: the three-bars button. All other functions
                 (Skills / Knowledge Graph / System Config / Extend) have been moved from the top
                 bar into the large dialog nine-grid that this button pops open. -->
            <Button
              icon="pi pi-bars"
              :title="t('toolbar.settingsMenu')"
              :aria-label="t('toolbar.settingsMenu')"
              variant="text"
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

    <!-- Dialogs are lazily loaded (defineAsyncComponent below): `v-if` is what makes the
         laziness real — an async component that is always rendered would fetch its chunk as
         soon as this page mounts. NotificationDialog is mounted permanently instead: its
         ws:notification subscription and unread badge must stay live while the dialog is
         closed, so the async chunk is still loaded off the critical path but the component
         never unmounts. Every other toolbar / settings-menu entry is a right-sidebar tab
         (see dialogs.ts), so those panels mount and unmount with their tab. -->

    <!-- Notification dialog (listens to ws:notification, merges consecutive identical
       notifications, reports the unread count via changed) -->
    <NotificationDialog
      v-model="dialogs.visible.notification"
      @changed="(n: number) => (notificationUnread = n)" />
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
import { ensureSessionCharacter } from './components/SessionSidebar.vue';
import ModeSwitch from './components/ModeSwitch.vue';
import AsyncChunkFallback from '@/components/AsyncChunkFallback.vue';
// function
import { computed, defineAsyncComponent, onMounted, type Component } from 'vue';
import { useI18n } from 'vue-i18n';
import { storeToRefs } from 'pinia';
import { useChatBackgroundStore } from '~/stores/chat-background';
import { headerTools } from './config';
import { buildHomeToolbarCommands, HOME_DIALOG_IDS } from './dialogs';

/**
 * Wrap a dialog `import()` in an async component.
 *
 * The remaining dialog (the notification list) is code-split so its module
 * graph is not part of the initial `/home` chunk. The `loadingComponent`
 * covers the first-open chunk fetch; `delay: 150` avoids a spinner flash on
 * fast (cached) loads.
 * @param loader Dynamic import of the dialog SFC
 */
const lazyDialog = (loader: () => Promise<{ default: Component }>) =>
  defineAsyncComponent({ loader, loadingComponent: AsyncChunkFallback, delay: 150 });

const NotificationDialog = lazyDialog(() => import('./components/NotificationDialog.vue'));

const { t, locale, setLocale } = useI18n();

/** Global chat area background image: bound to the root container (fills the entire window, including the left session list) */
const chatBackgroundStore = useChatBackgroundStore();
const { backgroundOpacity, chatBackgroundStyle, chatBackgroundOverlayStyle } = storeToRefs(chatBackgroundStore);

/** Language switcher options: reuses the language names from System Config (each locale maps to its own language name) */
const languageOptions = computed(() => [
  { name: t('config.language.zh'), code: 'zh' },
  { name: t('config.language.en'), code: 'en' },
  { name: t('config.language.ja'), code: 'ja' },
  { name: t('config.language.ko'), code: 'ko' }
]);

/**
 * Language switch handler: switches via nuxt-i18n's `setLocale`. Under the `no_prefix`
 * strategy, `setLocale`'s internal `navigate()` returns early, **without triggering a route
 * navigation**, so URLs of session views like `/home/:id` stay stable.
 *
 * `setLocale` also does two things at once:
 * - Loads the target locale's language pack (`mergeLocaleMessage`), avoiding rendering raw keys
 * - Writes the preference cookie (`i18n_redirected`) for persistence, so the chosen language
 *   can be restored after a refresh
 *
 * (Compared with directly setting `locale.value = code` + manually writing the cookie,
 *  `setLocale` is the only path that guarantees the language pack gets loaded; otherwise
 *  `$t` returns raw keys on first render/switch.)
 * @param code
 */
async function onLanguageChange(code: string) {
  await setLocale(code as 'zh' | 'en' | 'ja' | 'ko');
  persistLocalePreference(code as 'zh' | 'en' | 'ja' | 'ko');
}

/**
 * Persist the language preference cookie (key: i18n_redirected).
 *
 * Background: after nuxt.config.ts set `detectBrowserLanguage: false`, the module normalizes the
 * detection config to `{}`, which makes `setCookieLocale` a no-op because `detectConfig.useCookie`
 * is falsy — **the module never writes the cookie itself**.
 * As a result, `setLocale` can only switch immediately and cannot persist. To satisfy
 * "the preferred language survives browser refresh/restart", we must manually write the
 * preference cookie and have app.vue read it first on initial load (app.vue's read logic
 * cooperates using the same key).
 * @param code
 */
function persistLocalePreference(code: 'zh' | 'en' | 'ja' | 'ko') {
  if (import.meta.server) return;
  const pref = useCookie('i18n_redirected');
  pref.value = code;
}
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

/** Every dialog the shell owns, with its visibility flag + open/close actions (registry-driven) */
const dialogs = useDialogManager(HOME_DIALOG_IDS);

/**
 * Toolbar command registry: event → command (dialogs + right-sidebar tabs).
 * `handleOperate` is a lookup, so a new toolbar entry only needs a registry row.
 */
const toolbarCommands = buildHomeToolbarCommands({
  openDialog: dialogs.open,
  openRightTab: kind => rightSidebarStore.openTab(kind)
});

/** Notification badge unread count (reported by NotificationDialog) */
const notificationUnread = ref(0);

/** Global UI store (unified entry for sidebar collapse / settings menu / theme) */
const uiStore = useUiStore();
/** Whether the settings menu (nine-grid) is open (transient, not persisted) */
const { settingsMenuOpen: isSettingsMenuOpen } = storeToRefs(uiStore);

/** Whether the left history sidebar is collapsed (expanded by default; persisted to localStorage and restored after refresh) */
const { sidebarCollapsed: isSidebarCollapsed } = storeToRefs(uiStore);

/** Right sidebar (viewer + tool tabs): collapse flag + the tab actions the menu uses */
const rightSidebarStore = useRightSidebarStore();
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
});
</script>
