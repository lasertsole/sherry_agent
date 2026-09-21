import { describe, it, expect, vi } from 'vitest';
import { OutboundQueue } from '../bridge/agent-socket-queue';

describe('OutboundQueue', () => {
  it('flushes buffered frames in insertion order', () => {
    const queue = new OutboundQueue();
    queue.enqueue('a');
    queue.enqueue('b');
    queue.enqueue('c');
    const sent: string[] = [];
    queue.flush(frame => sent.push(frame));
    expect(sent).toEqual(['a', 'b', 'c']);
  });

  it('clears the buffer after a flush (no duplicate delivery)', () => {
    const queue = new OutboundQueue();
    queue.enqueue('a');
    const sent: string[] = [];
    queue.flush(frame => sent.push(frame));
    queue.flush(frame => sent.push(frame));
    expect(sent).toEqual(['a']);
  });

  it('accepts new frames after a flush', () => {
    const queue = new OutboundQueue();
    queue.enqueue('a');
    queue.flush(() => {});
    queue.enqueue('b');
    const sent: string[] = [];
    queue.flush(frame => sent.push(frame));
    expect(sent).toEqual(['b']);
  });

  it('drops buffered frames when the sink is a no-op (disposed socket)', () => {
    const queue = new OutboundQueue();
    queue.enqueue('a');
    const sink = vi.fn();
    queue.flush(sink);
    queue.enqueue('b');
    expect(sink).toHaveBeenCalledTimes(1);
  });
});
