/**
 * Native folder picker for the project-directory chip (Tauri desktop only).
 *
 * Loaded through a DYNAMIC import: the browser build resolves no Tauri plugins
 * at runtime, so a static import would break the web bundle. The manual input in
 * the chip is the browser fallback channel — this helper only adds the native
 * convenience where it exists.
 */

/** Whether this build runs inside the Tauri webview. */
export function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/**
 * Open the OS folder picker and return the chosen absolute path.
 *
 * Returns null when the user cancels or the runtime has no dialog plugin (the
 * caller keeps the manual-entry path in that case).
 */
export async function pickProjectDirectory(): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { open } = await import('@tauri-apps/plugin-dialog');
  const picked = await open({ directory: true, multiple: false });
  return typeof picked === 'string' && picked ? picked : null;
}
