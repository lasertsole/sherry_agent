/**
 * Install the browser `console.*` capture at startup.
 *
 * The capture feeds the Log Viewer's "Client" tab (live buffer + Dexie history)
 * and is a process-level singleton, so it must run from app startup. It used to
 * be installed by LogsDialog's setup; now that the dialog is lazily loaded on
 * first open, the capture has to be owned here or every log emitted before the
 * user opens the viewer would be lost. The `typeof window` guard keeps the
 * plugin inert outside the browser (the app is `ssr: false`).
 */
export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined') return;
  installClientLogCapture();
});
