import { describe, it, expect, vi } from 'vitest';
import { createWsMessageHandler, isWsObjectFrame } from '../ws-message';

const asMessageEvent = (data: string): MessageEvent => ({ data }) as MessageEvent;

describe('isWsObjectFrame', () => {
  it('accepts plain objects and rejects every other JSON shape', () => {
    expect(isWsObjectFrame({ event: 'ready' })).toBe(true);
    expect(isWsObjectFrame([{ event: 'ready' }])).toBe(false);
    expect(isWsObjectFrame(null)).toBe(false);
    expect(isWsObjectFrame(123)).toBe(false);
    expect(isWsObjectFrame('ready')).toBe(false);
    expect(isWsObjectFrame(true)).toBe(false);
    expect(isWsObjectFrame(undefined)).toBe(false);
  });
});

describe('createWsMessageHandler frame validation', () => {
  it('dispatches object frames by event and returns the parsed frame', () => {
    const handler = vi.fn();
    const onFrame = createWsMessageHandler<{ event: string; content?: string }>({ notification: handler });

    const frame = onFrame(asMessageEvent(JSON.stringify({ event: 'notification', content: 'hi' })));

    expect(frame).toEqual({ event: 'notification', content: 'hi' });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({ event: 'notification', content: 'hi' });
  });

  it('drops malformed JSON frames', () => {
    const handler = vi.fn();
    const onFrame = createWsMessageHandler({ notification: handler });

    expect(onFrame(asMessageEvent('not-json'))).toBeNull();
    expect(handler).not.toHaveBeenCalled();
  });

  it.each([
    ['null', 'null'],
    ['a number', '123'],
    ['a bare string', '"ready"'],
    ['a boolean', 'true'],
    ['an array', '[{"event":"notification"}]']
  ])('drops %s frames instead of returning them as T', (_label, raw) => {
    const handler = vi.fn();
    const onFrame = createWsMessageHandler({ notification: handler });

    expect(onFrame(asMessageEvent(raw))).toBeNull();
    expect(handler).not.toHaveBeenCalled();
  });

  it('returns an object frame without a known event for caller passthrough', () => {
    const onFrame = createWsMessageHandler({});
    expect(onFrame(asMessageEvent(JSON.stringify({ foo: 'bar' })))).toEqual({ foo: 'bar' });
  });
});
