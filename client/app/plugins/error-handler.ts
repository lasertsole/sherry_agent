/**
 * Install the global error handlers as early as possible in the app lifecycle
 * (before the first route component mounts), so errors thrown during startup
 * are captured too.
 *
 * The core logic lives in `composables/global-error-handler.ts` so it can be unit
 * tested without Nuxt. The `typeof window` guard keeps the plugin inert if it
 * ever runs outside the browser (the app is `ssr: false`, so in practice it
 * only executes client-side).
 */
export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined') return;
  installGlobalErrorHandler();
});
