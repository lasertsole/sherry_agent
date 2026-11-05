/**
 * `invokeNative`: an unavailable native command resolves to `null`.
 *
 * The contract is "native layer unavailable ⇒ caller takes the HTTP path". A
 * missing/unregistered command used to REJECT out of here, so the caller never
 * reached its HTTP fallback (the packaged memory panel showed a blank screen).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const invokeMock = vi.hoisted(() => vi.fn());

vi.mock('@tauri-apps/api/core', () => ({ invoke: invokeMock }));

import { invokeNative, isTauri } from '@/composables/bridge/transport';

const internals = (globalThis as any).window as any;

beforeEach(() => {
  invokeMock.mockReset();
  internals.__TAURI_INTERNALS__ = {};
});

afterEach(() => {
  delete internals.__TAURI_INTERNALS__;
});

describe('invokeNative', () => {
  it('detects the desktop shell', () => {
    expect(isTauri()).toBe(true);
  });

  it('returns the native value when the command succeeds', async () => {
    invokeMock.mockResolvedValue({ ok: 1 });

    await expect(invokeNative('memory_read')).resolves.toEqual({
      kind: 'native',
      value: { ok: 1 }
    });
  });

  it('resolves null when the command is missing or rejects', async () => {
    invokeMock.mockRejectedValue(new Error('command memory_read not found'));

    await expect(invokeNative('memory_read')).resolves.toBeNull();
  });

  it('resolves null in the browser build without touching IPC', async () => {
    delete internals.__TAURI_INTERNALS__;

    await expect(invokeNative('memory_read')).resolves.toBeNull();
    expect(invokeMock).not.toHaveBeenCalled();
  });
});
