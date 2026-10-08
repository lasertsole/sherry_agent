import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { Mock } from 'vitest';
import { emit, on } from '../mitt';

/** mitt listeners receive a single `unknown` payload (default Events map). */
type Handler = (event: unknown) => void;

describe('mitt event bus', () => {
  let handler: Mock<Handler>;

  beforeEach(() => {
    handler = vi.fn<Handler>();
  });

  afterEach(() => {
    handler.mockClear();
  });

  it('delivers a payload to subscribed listeners', () => {
    on('my:event', handler);
    emit('my:event', { hello: 'world' });

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({ hello: 'world' });
  });

  it('does not invoke a listener when a different event is emitted', () => {
    on('event-a', handler);
    emit('event-b', 'payload');

    expect(handler).not.toHaveBeenCalled();
  });

  it('delivers undefined payload when emit has a single argument', () => {
    on('bare:event', handler);
    emit('bare:event');

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith(undefined);
  });

  it('invokes multiple listeners for the same event in registration order', () => {
    const second = vi.fn<Handler>();
    on('multi', handler);
    on('multi', second);

    emit('multi', 'data');

    expect(handler).toHaveBeenCalledWith('data');
    expect(second).toHaveBeenCalledWith('data');
    expect(handler.mock.invocationCallOrder[0]! < second.mock.invocationCallOrder[0]!).toBe(true);
  });
});
