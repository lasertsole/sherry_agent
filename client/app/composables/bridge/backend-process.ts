/**
 * Backend process lifecycle bridge (Tauri desktop only).
 *
 * `restart_backend` stops the managed Python process and spawns it again:
 * the agent graph is rebuilt on boot, so anything the loader reads at startup
 * (new MCP servers in the drop folder, a freshly installed CUDA pack) takes
 * effect. A no-op outside the Tauri runtime (a browser build has no shell).
 *
 * The CUDA pack bridge carries its own `restartBackend` wrapper for the same
 * command — this module is the generic surface new callers should use.
 *
 * @module bridge/backend-process
 */
import { invokeNative } from './transport';

/**
 * Stop and restart the managed backend.
 * @returns `true` when a restart was requested; `false` outside the Tauri runtime.
 */
export async function restartBackendProcess(): Promise<boolean> {
  const result = await invokeNative<null>('restart_backend');
  return result !== null;
}
