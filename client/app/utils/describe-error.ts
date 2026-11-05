/**
 * Human-readable text for anything a catch block receives.
 *
 * Tauri IPC rejects with a PLAIN OBJECT (`FrontendError` = `{ code, message }`,
 * serialized by the Rust shell), not an `Error` instance. `String(value)` on
 * that object renders `"[object Object]"` — exactly what the first-run session
 * dialog showed for every failed preset. Prefer the structured message, keep
 * the code when there is one, and fall back to a JSON dump before String().
 *
 * @module utils/describe-error
 */

/**
 * Describe a thrown value without ever producing "[object Object]".
 * @param error The caught value (Error, Tauri error object, string, anything).
 * @returns A description; "" only for null/undefined.
 */
export function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (error && typeof error === 'object') {
    const { message, code } = error as { message?: unknown; code?: unknown };
    if (typeof message === 'string' && message.trim()) {
      return typeof code === 'string' && code.trim() ? `${code}: ${message}` : message;
    }
    try {
      const json = JSON.stringify(error);
      if (json && json !== '{}') return json;
    } catch {
      // Circular / unserializable: fall through to String().
    }
  }
  return error === undefined || error === null ? '' : String(error);
}
