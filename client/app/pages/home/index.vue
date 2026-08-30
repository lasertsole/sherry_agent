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
      <div
        class="flex items-center justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- Left: collapse/expand the history sidebar -->
        <Button
          :icon="isSidebarCollapsed ? 'pi pi-angle-double-right' : 'pi pi-angle-double-left'"
          :title="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
          :aria-label="isSidebarCollapsed ? t('toolbar.expandSidebar') : t('toolbar.collapseSidebar')"
          variant="text"
          class="text-theme-main"
          @click="toggleSidebar" />
        <!-- Right: original function button area -->
        <div class="flex items-center gap-3">
          <ModeSwitch />
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
              class="w-40"
              size="small"
              aria-label="Language / 语言"
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
            <!-- Logs entry: kept in the top bar (no matching nine-grid icon; not merged into the settings menu) -->
            <Button
              icon="pi pi-history"
              :title="t('toolbar.logs')"
              :aria-label="t('toolbar.logs')"
              variant="text"
              @click="handleOperate('headerBar', 'logs')" />
            <!-- Settings menu entry: the three-bars button. All other functions
                 (Skills / Knowledge Graph / System Config / Extend) have been moved from the top
                 bar into the large dialog nine-grid that this button pops open. -->
            <Button
              icon="pi pi-bars"
              :title="t('toolbar.settingsMenu')"
              :aria-label="t('toolbar.settingsMenu')"
              variant="text"
              @click="isSettingsMenuOpen = true" />
          </div>
        </div>
      </div>

      <!-- Settings menu: shown centered in a large dialog containing a nine-grid.
           Each function is a square block with a large icon on top and the function name below.
           Clicking an item directly triggers the corresponding function (dialog / route jump). -->
      <Dialog
        v-model:visible="isSettingsMenuOpen"
        :header="t('toolbar.settingsMenu')"
        :modal="true"
        :closable="true"
        class="w-[min(90vw,720px)]">
        <div class="grid grid-cols-3 gap-4">
          <button
            v-for="tool in headerTools"
            :key="tool.event"
            type="button"
            class="flex flex-col items-center justify-center gap-3 w-full h-32 rounded-xl border border-solid border-gray-light dark:border-gray-dark bg-gray-50 dark:bg-gray-800 hover:border-theme-main hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors cursor-pointer"
            :title="t(tool.title)"
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
      <div class="flex-1 min-h-0">
        <NuxtPage
          :page-key="resolvePageKey"
          :keepalive="{ max: KEEP_ALIVE_MAX }" />
      </div>

      <!-- Skills dialog -->
      <SkillsDialog v-model="showSkillsDialog" />

      <!-- Statistics dialog -->
      <StatsDialog v-model="showStatsDialog" />

      <!-- System config dialog -->
      <ConfigDialog
        v-model="showConfigDialog"
        @saved="loadCharacter" />

      <!-- AI persona dialog -->
      <PersonaDialog v-model="showPersonaDialog" />

      <!-- Memory dialog -->
      <MemoryDialog v-model="showMemoryDialog" />

      <!-- Heartbeat tasks dialog -->
      <HeartbeatDialog v-model="showHeartbeatDialog" />

      <!-- Cron (scheduled tasks) dialog -->
      <CronDialog v-model="showCronDialog" />

      <!-- Logs dialog -->
      <LogsDialog v-model="showLogsDialog" />

      <!-- Notification dialog (listens to ws:notification, merges consecutive identical
         notifications, reports the unread count via changed) -->
      <NotificationDialog
        v-model="showNotificationDialog"
        @changed="(n: number) => (notificationUnread = n)" />

      <!-- Extend dialog (integrations / mcp) -->
      <ExtendDialog v-model="showExtendDialog" />
    </div>
  </div>
</template>

<script lang="ts" setup>
// Page-level error capture: runtime errors from all descendant components on this page
// (sidebar/toolbar/each dialog, plus child route pages without their own capture)
// → logUtil logging + global toast; returning false stops further upward propagation
// (factory function pattern from 03-errorCapturedFactoryFunction.md)
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();

// components
import SessionSidebar from './components/SessionSidebar.vue';
import { ensureSessionCharacter } from './components/SessionSidebar.vue';
import ModeSwitch from './components/ModeSwitch.vue';
import SkillsDialog from './components/SkillsDialog.vue';
import StatsDialog from './components/StatsDialog.vue';
import ConfigDialog from './components/ConfigDialog.vue';
import PersonaDialog from './components/PersonaDialog.vue';
import MemoryDialog from './components/MemoryDialog.vue';
import HeartbeatDialog from './components/HeartbeatDialog.vue';
import CronDialog from './components/CronDialog.vue';
import LogsDialog from './components/LogsDialog.vue';
import ExtendDialog from './components/ExtendDialog.vue';
import NotificationDialog from './components/NotificationDialog.vue';
// function
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { headerTools } from './config';

const { t, locale, setLocale } = useI18n();

/** Global chat area background image: bound to the root container (fills the entire window, including the left session list) */
const { backgroundOpacity, chatBackgroundStyle, chatBackgroundOverlayStyle, loadBackground } = useChatBackground();

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
 */
function persistLocalePreference(code: 'zh' | 'en' | 'ja' | 'ko') {
  if (import.meta.server) return;
  const pref = useCookie('i18n_redirected');
  pref.value = code;
}
const router = useRouter();
const localePath = useLocalePath();

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
 */
const resolvePageKey = (route: { path: string; params: Record<string, unknown> }) => {
  const sid = String(route.params.sid ?? 'root');
  return route.path.includes('/tasks/') ? `tasks-${sid}` : sid;
};

/** Skills dialog toggle */
const showSkillsDialog = ref(false);

/** Statistics dialog toggle */
const showStatsDialog = ref(false);

/** System config dialog toggle */
const showConfigDialog = ref(false);

/** AI persona dialog toggle */
const showPersonaDialog = ref(false);

/** Memory dialog toggle */
const showMemoryDialog = ref(false);

/** Heartbeat tasks dialog toggle */
const showHeartbeatDialog = ref(false);

/** Cron (scheduled tasks) dialog toggle */
const showCronDialog = ref(false);

/** Logs dialog toggle */
const showLogsDialog = ref(false);

/** Extend dialog toggle */
const showExtendDialog = ref(false);

/** Notification dialog toggle */
const showNotificationDialog = ref(false);

/** Notification badge unread count (reported by NotificationDialog) */
const notificationUnread = ref(0);

/** Global UI store (unified entry for sidebar collapse / settings menu / theme) */
const uiStore = useUiStore();
/** Whether the settings menu (nine-grid) is open (transient, not persisted) */
const { settingsMenuOpen: isSettingsMenuOpen } = storeToRefs(uiStore);

/** Whether the left history sidebar is collapsed (expanded by default; persisted to localStorage and restored after refresh) */
const { sidebarCollapsed: isSidebarCollapsed } = storeToRefs(uiStore);

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

/** Tool trigger (header bar only; toolbar/images etc. have moved into [sid].vue along with the session main area) */
const handleOperate = (type: string, event: string) => {
  if (!event || type !== 'headerBar') return;
  switch (event) {
    case 'skills':
      showSkillsDialog.value = true;
      return;
    case 'knowledgeGraph':
      router.push(localePath('/knowledge-graph'));
      return;
    case 'stats':
      showStatsDialog.value = true;
      return;
    case 'systemConfig':
      showConfigDialog.value = true;
      return;
    case 'persona':
      showPersonaDialog.value = true;
      return;
    case 'memory':
      showMemoryDialog.value = true;
      return;
    case 'heartbeat':
      showHeartbeatDialog.value = true;
      return;
    case 'cron':
      showCronDialog.value = true;
      return;
    case 'logs':
      showLogsDialog.value = true;
      return;
    case 'notification':
      showNotificationDialog.value = true;
      return;
    case 'extend':
      showExtendDialog.value = true;
      return;
    default:
      return;
  }
};

/**
 * Settings menu (nine-grid) item click handler: first triggers the corresponding tool event,
 * then collapses the menu.
 * knowledgeGraph is a route jump while the rest are dialogs; both uniformly reuse
 * handleOperate's event dispatch.
 */
const handleMenuSelect = (event: string) => {
  isSettingsMenuOpen.value = false;
  handleOperate('headerBar', event);
};

/** Collapse/expand the left history sidebar */
const toggleSidebar = () => {
  isSidebarCollapsed.value = !isSidebarCollapsed.value;
};

// Load the global chat area background image after mount (session list fetching is already
// done inside the SessionSidebar component)
onMounted(() => {
  loadBackground();
});
</script>
