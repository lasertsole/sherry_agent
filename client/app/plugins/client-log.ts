/**
 * Install the browser `console.*` capture at startup.
 *
 * The capture feeds the Log Viewer's "Client" tab (live buffer + Dexie history)
 * and is a process-level singleton, so it must run from app startup: the log
 * panel only mounts when its tab is opened, and logs emitted before that would
 * otherwise be lost. The `typeof window` guard keeps the plugin inert outside
 * the browser (the app is `ssr: false`).
 */
export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined') return;
  installClientLogCapture();
});
