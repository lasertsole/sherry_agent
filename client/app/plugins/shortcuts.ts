// The two symbols are Nuxt auto-imports (app/composables/use-shortcuts.ts).

/**
 * Install the ONE document listener that dispatches global shortcut chords, and
 * load the user's overrides from IndexedDB before the first press.
 *
 * The bindings themselves are not registered here: each owner calls
 * `registerGlobalHandler(id, fn)` for the actions it can perform, so the shell
 * (palette / new session / sidebar) and a session page (find in conversation)
 * contribute independently and a rebind changes the keys, not the wiring.
 */
export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined') return; // SSR: no document to listen on
  void loadShortcutBindings();
  const uninstall = installGlobalShortcutListener();
  // Vite HMR would otherwise stack one listener per plugin re-run, firing every
  // chord twice.
  import.meta.hot?.dispose(uninstall);
});
