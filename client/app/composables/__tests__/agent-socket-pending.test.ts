import { describe, it, expect, vi } from 'vitest';
import { PendingSendRegistry } from '../bridge/agent-socket-pending';
import type { ChatRequest } from '../bridge/chat-types';

const request = (over: Partial<ChatRequest> = {}): ChatRequest => ({ text: 'hi', ...over });

describe('PendingSendRegistry', () => {
  it('registers a send and resolves it once, removing it from the registry', async () => {
    const reg = new PendingSendRegistry();
    const { pending, promise } = reg.create('m1', request());
    expect(pending.settled).toBe(false);
    expect(reg.get('m1')).toBe(pending);

    pending.resolve();
    await expect(promise).resolves.toBeUndefined();
    expect(pending.settled).toBe(true);
    expect(reg.get('m1')).toBeUndefined();
    expect(reg.keys()).toEqual([]);

    // Settle-once: a second resolve is a no-op, never throws.
    pending.resolve();
  });

  it('normalizes a non-Error rejection value', async () => {
    const reg = new PendingSendRegistry();
    const { pending, promise } = reg.create('m1', request());
    pending.reject('boom');
    await expect(promise).rejects.toThrow('boom');
  });

  it('settleResolve targets only the given ids; an empty list settles every pending send', async () => {
    const reg = new PendingSendRegistry();
    const a = reg.create('a', request());
    const b = reg.create('b', request());
    reg.settleResolve(['a']);
    await expect(a.promise).resolves.toBeUndefined();
    expect(b.pending.settled).toBe(false);

    reg.settleResolve([]);
    await expect(b.promise).resolves.toBeUndefined();
  });

  it('settleReject rejects with the shared error and falls back to all pending sends', async () => {
    const reg = new PendingSendRegistry();
    const a = reg.create('a', request());
    const err = new Error('x');
    reg.settleReject([], err);
    await expect(a.promise).rejects.toBe(err);
  });

  it('abandonAll drops sends WITHOUT settling their promises', async () => {
    const reg = new PendingSendRegistry();
    const { pending, promise } = reg.create('m1', request());
    reg.abandonAll();
    expect(pending.settled).toBe(true);
    expect(reg.keys()).toEqual([]);

    const settled = vi.fn();
    promise.then(settled, settled);
    pending.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).not.toHaveBeenCalled();
  });

  it('resolveAll resolves every registered send (teardown)', async () => {
    const reg = new PendingSendRegistry();
    const a = reg.create('a', request());
    const b = reg.create('b', request());
    reg.resolveAll();
    await expect(a.promise).resolves.toBeUndefined();
    await expect(b.promise).resolves.toBeUndefined();
    expect(reg.keys()).toEqual([]);
  });
});
