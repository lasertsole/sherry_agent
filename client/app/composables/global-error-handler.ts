import { logUtil } from '~/utils/log';

/**
 * Global (non-Vue) error capture.
 *
 * Vue's `onErrorCaptured` only sees errors raised inside the component tree. Two
 * classes of errors escape it entirely and were previously lost with no trace:
 *
 * - uncaught runtime errors from `setTimeout` / `setInterval` callbacks, module
 *   top-level code, or event handlers outside Vue's call stack;
 * - unhandled promise rejections (the ~30 fire-and-forget `void asyncFn()`
 *   call sites in this app reject into nothing).
 *
 * Both are routed into the shared log channel (`logUtil.e` → `console.error`,
 * which `clientLog.ts` persists to IndexedDB and the Log Viewer displays),
 * mirroring how `useErrorCaptured` reports component errors. No toast is shown:
 * background failures must not pop global error dialogs.
 *
 * The listeners intentionally do NOT use `{ capture: true }`: resource load
 * failures (a broken `<img>`/`<audio>` src) only surface at the window in the
 * capture phase, and they are already handled per-component (e.g. ChatBox's
 * `failedImageSources`), so capturing them here would only add noise.
 */

/**
 * Normalize any thrown/rejected value into readable log text.
 * @param value
 */
function describeError(value: unknown): string {
  if (value instanceof Error) {
    return value.stack ?? `${value.name}: ${value.message}`;
  }
  if (typeof value === 'string') {
    return value;
  }
  try {
    const json = JSON.stringify(value);
    return json === undefined ? String(value) : json;
  } catch {
    // JSON.stringify throws on circular structures
    return String(value);
  }
}

/** Cleanup handle of the currently installed listeners (idempotent install). */
let activeCleanup: (() => void) | null = null;

/**
 * Install the global `error` / `unhandledrejection` listeners on the given window.
 *
 * Idempotent: repeated calls return the existing cleanup handle instead of
 * stacking duplicate listeners (Nuxt HMR can re-run the client plugin).
 *
 * @param target Window to attach to (injectable for tests; defaults to `window`)
 * @returns Cleanup that removes both listeners; installing again afterwards is allowed
 */
export function installGlobalErrorHandler(target: Window = window): () => void {
  if (activeCleanup) {
    return activeCleanup;
  }

  const onWindowError = (event: ErrorEvent): void => {
    const location = event.filename ? ` (${event.filename}:${event.lineno}:${event.colno})` : '';
    if (event.error instanceof Error) {
      logUtil.e(`[global] Uncaught error:${location}`, describeError(event.error));
      return;
    }
    logUtil.e(`[global] Uncaught error:${location}`, event.message || 'Unknown error');
  };

  const onUnhandledRejection = (event: PromiseRejectionEvent): void => {
    logUtil.e('[global] Unhandled promise rejection:', describeError(event.reason));
  };

  target.addEventListener('error', onWindowError);
  target.addEventListener('unhandledrejection', onUnhandledRejection);

  const cleanup = (): void => {
    target.removeEventListener('error', onWindowError);
    target.removeEventListener('unhandledrejection', onUnhandledRejection);
    if (activeCleanup === cleanup) {
      activeCleanup = null;
    }
  };
  activeCleanup = cleanup;
  return cleanup;
}
