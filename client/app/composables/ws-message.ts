/**
 * Narrow runtime guard for a parsed WebSocket frame.
 *
 * `JSON.parse` accepts any JSON value, so a malformed/misbehaving peer could
 * deliver `null`, a number, a string or an array where every consumer expects
 * an object (`data.event`, `data.content`, `data.data`, ...). Rejecting
 * non-object frames here keeps the `as T` assertions inside the callers honest
 * (audit #51).
 * @param value Parsed JSON value
 */
export function isWsObjectFrame(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Parse one WebSocket JSON frame and route it by its `event` field.
 *
 * The returned handler is a drop-in `socket.onmessage` implementation:
 * - non-JSON frames are dropped (returns `null`),
 * - JSON frames that are not plain objects are dropped (returns `null`),
 * - frames carrying an `event` field trigger the matching handler,
 * - frames without a matching handler are ignored (dispatch-wise) and the
 *   parsed data is still returned so the caller can apply its own passthrough.
 * @param handlers Handlers keyed by the frame's `event` field
 * @returns A `socket.onmessage`-compatible handler returning the parsed frame (or `null` when parsing/validation failed)
 */
export function createWsMessageHandler<T = Record<string, unknown>>(
  handlers: Record<string, (data: T) => void>
): (event: MessageEvent) => T | null {
  return (event: MessageEvent): T | null => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(event.data as string);
    } catch {
      return null;
    }
    if (!isWsObjectFrame(parsed)) {
      return null;
    }
    const data = parsed as T;
    const type = (data as { event?: unknown }).event;
    if (typeof type === 'string') handlers[type]?.(data);
    return data;
  };
}
