<template>
  <NuxtLayout>
    <!-- Top-level <NuxtPage> no longer wraps keepalive: per-session state is cached by the
         KeepAlive of the inner <NuxtPage :page-key="route.params.sid"> inside home/index.vue,
         keyed by page-key.
         If both layers keepalive at once (nested KeepAlive), when switching sessions the outer
         layer caches by route name (single slot for home/index) while the inner layer swaps child
         pages by page-key; the two rhythms disagree and the first frame can show a misaligned
         ghost of "old and new child pages briefly coexisting". Removing the redundant outer
         keepalive eliminates this race. -->
    <NuxtPage />
  </NuxtLayout>
  <ImagePreviewOverlay />
  <Toast position="bottom-right" />
  <!-- Global confirm dialog (ConfirmationService is auto-registered along with components by
       @primevue/nuxt-module): destructive operations such as deleting a session/background task
       all go through useConfirm(), replacing native window.confirm -->
  <ConfirmDialog />
  <!-- Global connection status banner when offline / backend unreachable (red = offline;
       amber = network online but backend unreachable) -->
  <Transition name="conn-fade">
    <div
      v-if="showConnectionBanner"
      class="conn-banner"
      :class="{ 'conn-banner--warn': isOnline !== false && backendStatus === 'down' }"
      role="status"
      aria-live="polite">
      {{ connectionBannerText }}
    </div>
  </Transition>
</template>

<script lang="ts" setup>
// Client-side initialization restores the selected locale (paired with detectBrowserLanguage: false in nuxt.config.ts).
// Background: Nuxt i18n's browser-language auto detection under prefix_except_default would auto-redirect
// /home/:id to /en/home/:id (the prefixed route does not exist), breaking i18n, so it has been fully
// disabled in the config. We take over here ourselves:
//   1) First read the persisted preference cookie (key: i18n_redirected, the default key written by
//      the nuxt-i18n module's setLocale) — the user's last chosen locale has the highest priority;
//   2) Next match the browser language against valid locales (zh/en/ja/ko);
//   3) If nothing matches, fall back to English (en) by default.
// Everything goes through setLocale(): it both loads that locale's language pack (avoiding rendering
// raw keys on the first frame) and, when no preference cookie exists, writes the preference cookie,
// so the language is restored after refresh and the route stays stable.
const { setLocale, t } = useI18n();

// Global toast notification layer: injects the instance returned by useToast() into the toast
// registry so modules like requestApi / connection can show toasts outside component context
// (e.g. request interceptors).
registerToastApi(useToast());

// Top-level fallback for page-level error capture (03-errorCaptured factory doc §3.3, App-layer capture):
// captures errors from child components outside NuxtPage (ImagePreviewOverlay / connection banner /
// layout, etc.).
// Each page already calls useErrorCaptured() in its own setup to capture per page (return false stops
// propagation, so in-page errors never reach here); errorCaptured.ts's toast depends on registerToastApi
// on the line above.
useErrorCaptured();

// Network / backend connectivity monitoring (isOnline, backendStatus, startConnectionWatch, etc.).
const { isOnline, backendStatus, startConnectionWatch } = useConnection();

// Connection status banner: shown only when the browser is offline or the backend is unreachable.
const showConnectionBanner = computed(() => {
  return isOnline.value === false || backendStatus.value === 'down';
});
const connectionBannerText = computed(() => {
  if (isOnline.value === false) return t('connection.offline');
  return t('connection.backendDown');
});

// Preference cookie key used by nuxt-i18n by default when detectBrowserLanguage: false
const PREF_COOKIE = 'i18n_redirected';
const LOCALE_CODES = ['zh', 'en', 'ja', 'ko'] as const;
type LocaleCode = (typeof LOCALE_CODES)[number];

// Default fallback language when the browser does not match (requirement: default to English when the system language does not match)
const DEFAULT_LOCALE = 'en' as const;

onMounted(async () => {
  if (!import.meta.client) return;

  // Start the network/backend connectivity polling monitor (drives the global connection status
  // banner + deduplicated toasts).
  // The returned stop handle needs no saving: app.vue is the root component and shares the page's
  // lifecycle.
  void startConnectionWatch();

  // 1) Cookie: the user's last chosen language (highest priority, persisted preference)
  const cookieLocale = readCookie(PREF_COOKIE);
  if (cookieLocale && (LOCALE_CODES as readonly string[]).includes(cookieLocale)) {
    await setLocale(cookieLocale as LocaleCode);
    return;
  }

  // 2) Browser language (first visit), taking only the first two characters matched against zh/en/ja/ko
  const browserLang = navigator.language?.toLowerCase().slice(0, 2) ?? '';
  const matched = (LOCALE_CODES as readonly string[]).find(c => c === browserLang);
  if (matched) {
    await setLocale(matched as LocaleCode);
    persistLocalePreference(matched as LocaleCode);
    return;
  }

  // 3) Browser language matches no locale -> fall back to default English
  await setLocale(DEFAULT_LOCALE);
  persistLocalePreference(DEFAULT_LOCALE);
});

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

/**
 * Manually writes the language preference cookie (key: i18n_redirected).
 *
 * After nuxt.config.ts sets `detectBrowserLanguage: false`, the module normalizes the detection
 * config to `{}`, making `setCookieLocale` a no-op because `detectConfig.useCookie` is falsy —
 * the module never writes the cookie itself.
 * Therefore `setLocale` can only switch immediately and cannot persist. To satisfy "still the
 * preferred language after refresh/reopen", we write the preference cookie manually (same key set
 * as readCookie on first load; behavior consistent with persistLocalePreference in home/index.vue).
 */
function persistLocalePreference(code: LocaleCode) {
  if (!import.meta.client) return;
  // Synchronous write via document.cookie; same-site path, roughly one-year max-age
  document.cookie = `${PREF_COOKIE}=${encodeURIComponent(code)}; path=/; max-age=31536000; SameSite=Lax`;
}
</script>

<style lang="scss" scoped>
/* Global connection status banner (fixed to the bottom, does not squeeze the layout) */
.conn-banner {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 50;
  padding: 0.5rem 1rem;
  text-align: center;
  font-size: 0.875rem;
  font-weight: 500;
  color: #fff;
  background-color: #dc2626; /* red-600: browser offline */
  box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);
}

/* Network online but backend unreachable: step down one level to amber, reducing "offline" panic */
.conn-banner--warn {
  background-color: #d97706; /* amber-600 */
}

/* Banner fade in/out (paired with <Transition name="conn-fade">) */
.conn-fade-enter-active,
.conn-fade-leave-active {
  transition: opacity 0.25s ease;
}
.conn-fade-enter-from,
.conn-fade-leave-to {
  opacity: 0;
}
</style>
