/**
 * Transport infrastructure shared by the bridge domain modules.
 *
 * Holds the runtime detection and the two transport seams:
 * - `invokeNative`: the Tauri IPC path used by calls that mirror REST endpoints,
 *   resolving `null` when the browser transport is active (Strategy seam);
 * - `teardownWebSocket`: socket teardown shared by every WebSocket-backed call.
 *
 * @module bridge/transport
 */

/**
 * Returns `true` when running inside the Tauri desktop shell.
 */
export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

// ── Lazy Tauri imports ───────────────────────────────────

export async function getInvoke() {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke;
}

export async function getListen() {
  const { listen } = await import('@tauri-apps/api/event');
  return listen;
}

/** Result of a native IPC attempt: `null` = browser transport (no IPC path); otherwise the command's raw result. */
export type NativeInvokeResult<T> = { kind: 'native'; value: T } | null;

/**
 * Native transport half of the strategy: run a Tauri IPC command when the app
 * runs inside the Tauri shell, resolving `null` otherwise so the caller takes
 * the browser (direct HTTP) path.
 * @param command
 * @param args
 */
export async function invokeNative<T>(command: string, args?: Record<string, unknown>): Promise<NativeInvokeResult<T>> {
  if (!isTauri()) return null;
  const invoke = await getInvoke();
  return { kind: 'native', value: await invoke<T>(command, args) };
}

/**
 * Detach every event handler from `ws` and close it while still open/connecting,
 * so a torn-down socket can neither fire callbacks nor keep the connection alive.
 * @param ws
 */
export function teardownWebSocket(ws: WebSocket | null): void {
  if (!ws) return;
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
  if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
    ws.close();
  }
}
