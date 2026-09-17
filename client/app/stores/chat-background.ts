import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import { logUtil } from '~/utils/log';

/**
 * Chat-area background image store.
 *
 * The background image is a **global** (not per-session) setting: the user uploads it
 * in "System Settings → Background Settings", which writes to Dexie's single global
 * row (GLOBAL_SESSION_KEY). Because the store is a singleton, "saving takes effect
 * immediately, without a refresh" — the root container's binding and the dialog's save
 * both talk to the same reactive state.
 *
 * - `loadBackground()`: idempotent; the first call reads from Dexie and fills the
 *   store state (called from components in onMounted).
 * - `setBackground(url, opacity)`: updates the store state synchronously +
 *   persists to Dexie; called by ConfigDialog on save (after writing, every component
 *   sharing the singleton reacts immediately, no refresh needed). Passing an empty
 *   string clears the background.
 * - `chatBackgroundStyle`: reactive style object; returns a background-image in
 *   **both light and dark themes** when a background image exists (the photo is shown
 *   in dark mode too).
 * - `chatBackgroundOverlayStyle`: reactive overlay style. White overlay in the light
 *   theme, black in the dark theme, `opacity` = slider value/100 — the higher the
 *   value, the more the photo is washed out toward pure white/pure black, until fully
 *   obscured.
 *
 * Note: `useColorMode()` depends on the Nuxt setup context, so it must be called
 * inside the store setup function body (like `stores/ui.ts`) — the first
 * `useChatBackgroundStore()` call happens in a component's setup, and the captured
 * `colorMode` reference stays valid afterwards.
 */

export const useChatBackgroundStore = defineStore('chat-background', () => {
  const colorMode = useColorMode();

  const backgroundUrl = ref('');
  const backgroundOpacity = ref(0);
  const backgroundLoaded = ref(false);

  /** Chat-area background style: applies background-image in both light/dark themes when a background image exists */
  const chatBackgroundStyle = computed(() => {
    if (!backgroundUrl.value) return undefined;
    return {
      backgroundImage: `url("${backgroundUrl.value}")`,
      backgroundSize: 'cover',
      backgroundPosition: 'center',
      backgroundRepeat: 'no-repeat'
    };
  });

  /** Chat-area overlay style: light=white / dark=black, opacity grows with the slider — "the fuller it gets, the whiter/blacker until the photo is obscured" */
  const chatBackgroundOverlayStyle = computed(() => {
    const overlayColor = colorMode.value === 'light' ? '#ffffff' : '#000000';
    return {
      backgroundColor: overlayColor,
      opacity: backgroundOpacity.value / 100
    };
  });

  /** Idempotent load: reads Dexie on the first call and fills the store state */
  async function loadBackground(): Promise<void> {
    if (backgroundLoaded.value) return;
    try {
      const cfg = await readBackgroundConfig();
      backgroundUrl.value = cfg?.backgroundUrl ?? '';
      backgroundOpacity.value = cfg?.backgroundOpacity ?? 0;
    } catch (e) {
      logUtil.e('[useChatBackground] Failed to load background:', e);
    } finally {
      backgroundLoaded.value = true;
    }
  }

  /**
   * Update the shared state synchronously + persist to Dexie. Passing an empty string
   * clears the background. Persistence failures do not throw (a local frontend cache
   * failure must not block the save flow).
   * @param url
   * @param opacity
   */
  async function setBackground(url: string, opacity: number = backgroundOpacity.value): Promise<void> {
    backgroundUrl.value = url;
    backgroundOpacity.value = opacity;
    try {
      await saveBackground(url, opacity);
    } catch (e) {
      logUtil.e('[useChatBackground] Failed to save background:', e);
    }
  }

  return {
    backgroundUrl,
    backgroundOpacity,
    backgroundLoaded,
    chatBackgroundStyle,
    chatBackgroundOverlayStyle,
    loadBackground,
    setBackground
  };
});
