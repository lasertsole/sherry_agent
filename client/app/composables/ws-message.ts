/**
 * Parse one WebSocket JSON frame and route it by its `event` field.
 *
 * The returned handler is a drop-in `socket.onmessage` implementation:
 * - non-JSON frames are dropped (returns `null`),
 * - frames carrying an `event` field trigger the matching handler,
 * - frames without a matching handler are ignored (dispatch-wise) and the
 *   parsed data is still returned so the caller can apply its own passthrough.
 * @param handlers Handlers keyed by the frame's `event` field
 * @returns A `socket.onmessage`-compatible handler returning the parsed frame (or `null` when parsing failed)
 */
export function createWsMessageHandler<T = Record<string, unknown>>(
  handlers: Record<string, (data: T) => void>
): (event: MessageEvent) => T | null {
  return (event: MessageEvent): T | null => {
    let data: T;
    try {
      data = JSON.parse(event.data as string) as T;
    } catch {
      return null;
    }
    const type = (data as { event?: unknown } | null)?.event;
    if (typeof type === 'string') handlers[type]?.(data);
    return data;
  };
}
