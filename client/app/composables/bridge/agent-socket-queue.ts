/**
 * Outbound frame queue for the per-session agent socket.
 *
 * Frames produced while the socket is not OPEN are buffered and flushed in
 * insertion order once it opens. Frames enqueued while the connection is torn
 * down are dropped on flush (the send callback is a no-op then), matching the
 * previous in-class queue.
 *
 * @module bridge/agent-socket-queue
 */
export class OutboundQueue {
  private frames: string[] = [];

  /**
   * Buffer a frame for the next OPEN socket.
   * @param frame
   */
  enqueue(frame: string): void {
    this.frames.push(frame);
  }

  /**
   * Send every buffered frame in insertion order and clear the buffer.
   * @param send Sink invoked once per frame (may be a no-op when disconnected)
   */
  flush(send: (frame: string) => void): void {
    const queued = this.frames;
    this.frames = [];
    for (const frame of queued) send(frame);
  }
}
