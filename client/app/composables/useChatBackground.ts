import { computed, ref } from 'vue';
import { readBackgroundConfig, saveBackground } from '@/composables/db';

/**
 * Shared singleton for the chat-area background image (module-level reactive state).
 *
 * The background image is a **global** (not per-session) setting: the user uploads it
 * in "System Settings → Background Settings", which writes to Dexie's single global
 * row (GLOBAL_SESSION_KEY). So that "saving takes effect immediately, without a
 * refresh", the background state must be a **true module-level singleton** —
 * `backgroundUrl`/`backgroundOpacity`/`backgroundLoaded` are declared at the module
 * top level (outside the function); every call to `useChatBackground()` returns a
 * reference to the **same** refs, not separate copies.
 *
 * Otherwise (if the refs were declared inside the function), each call would create
 * its own independent state: the ref bound by the `home/index.vue` root container and
 * the ref modified when `ConfigDialog.vue` saves would be two isolated sets; saving
 * would only update the dialog's own copy, the root container's binding would not
 * change → the background would not refresh immediately, requiring a full page reload
 * that re-reads from Dexie to take effect. This was exactly the root cause of the
 * former "refresh required after saving" bug.
 *
 * Note: `colorMode` must be called inside the function (`useColorMode()` depends on
 * the Nuxt setup context), so the two computeds `chatBackgroundStyle`/
 * `chatBackgroundOverlayStyle` must also stay inside the function — the
 * `backgroundUrl`/`backgroundOpacity` they read are module-level shared; each call
 * site holds its own computed, but both reflect the same refs, so they still update
 * reactively, consistently and immediately.
 *
 * - `loadBackground()`: idempotent; the first call reads from Dexie and fills the
 *   singleton state (called from components in onMounted).
 * - `setBackground(url, opacity)`: updates the singleton state synchronously +
 *   persists to Dexie; called by ConfigDialog on save (after writing, every component
 *   sharing the singleton reacts immediately, no refresh needed).
 * - `setBackgroundOpacity(opacity)`: updates only the opacity + persists (keeps the
 *   current background image).
 * - `chatBackgroundStyle`: reactive style object; returns a background-image in
 *   **both light and dark themes** when a background image exists (the photo is shown
 *   in dark mode too).
 * - `chatBackgroundOverlayStyle`: reactive overlay style. White overlay in the light
 *   theme, black in the dark theme, `opacity` = slider value/100 — the higher the
 *   value, the more the photo is washed out toward pure white/pure black, until fully
 *   obscured.
 */

// ── Module-level shared state (the true singleton) ──
// All useChatBackground() callers share these same refs; changes via setBackground
// take effect globally and immediately.
const backgroundUrl = ref('');
const backgroundOpacity = ref(0);
const backgroundLoaded = ref(false);

/** Module-level idempotent load: reads Dexie on the first call and fills the singleton state */
const loadBackground = async () => {
  if (backgroundLoaded.value) return;
  try {
    const cfg = await readBackgroundConfig();
    backgroundUrl.value = cfg?.backgroundUrl ?? '';
    backgroundOpacity.value = cfg?.backgroundOpacity ?? 0;
  } catch (e) {
    console.error('[useChatBackground] Failed to load background:', e);
  } finally {
    backgroundLoaded.value = true;
  }
};

/**
 * Module-level setter: updates the shared singleton state synchronously + persists to
 * Dexie. Passing an empty string clears the background. Persistence failures do not
 * throw (a local frontend cache failure must not block the save flow).
 */
const setBackground = async (url: string, opacity: number = backgroundOpacity.value) => {
  backgroundUrl.value = url;
  backgroundOpacity.value = opacity;
  try {
    await saveBackground(url, opacity);
  } catch (e) {
    console.error('[useChatBackground] Failed to save background:', e);
  }
};

/** Module-level overlay-opacity update (keeps the current background image), persisted to Dexie */
const setBackgroundOpacity = async (opacity: number) => {
  backgroundOpacity.value = opacity;
  try {
    await saveBackground(backgroundUrl.value, opacity);
  } catch (e) {
    console.error('[useChatBackground] Failed to save background opacity:', e);
  }
};

export function useChatBackground() {
  const colorMode = useColorMode();

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

  return {
    backgroundUrl,
    backgroundOpacity,
    backgroundLoaded,
    loadBackground,
    setBackground,
    setBackgroundOpacity,
    chatBackgroundStyle,
    chatBackgroundOverlayStyle
  };
}
