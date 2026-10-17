/**
 * Desktop-only file actions: reveal in the OS file manager and "open with the
 * system default application" (image viewer, PDF reader, …).
 *
 * Both are Tauri capabilities — a browser page cannot reach the OS file
 * manager, so the web build simply does not offer them (`isTauriRuntime()`
 * gates the menu entries). The plugins load through DYNAMIC imports for the
 * same reason the folder picker does: the web bundle must not resolve them.
 *
 * @module utils/desktop-file-actions
 */
import { isTauriRuntime } from './project-directory';

/**
 * Whether the desktop-only actions are available in this runtime.
 */
export function canUseDesktopFileActions(): boolean {
  return isTauriRuntime();
}

/**
 * Show a path in the OS file manager (Finder / Explorer / the Linux file
 * manager), selecting the item itself.
 *
 * Returns false when the runtime has no opener plugin or the call is refused
 * (the caller then surfaces its own message instead of a silent no-op).
 * @param path Absolute path of the file or directory to reveal.
 */
export async function revealPathInFileManager(path: string): Promise<boolean> {
  if (!isTauriRuntime() || !path) return false;
  try {
    const { revealItemInDir } = await import('@tauri-apps/plugin-opener');
    await revealItemInDir(path);
    return true;
  } catch {
    return false;
  }
}

/**
 * Open a path with the OS default application (what a double-click in the file
 * manager would do).
 * @param path Absolute path to open.
 */
export async function openPathWithSystemApp(path: string): Promise<boolean> {
  if (!isTauriRuntime() || !path) return false;
  try {
    const { openPath } = await import('@tauri-apps/plugin-opener');
    await openPath(path);
    return true;
  } catch {
    return false;
  }
}

/** The OS the desktop build runs on (drives the "reveal" label). */
export function desktopPlatform(): 'macos' | 'windows' | 'linux' {
  const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent;
  if (/Mac/i.test(ua)) return 'macos';
  if (/Win/i.test(ua)) return 'windows';
  return 'linux';
}
