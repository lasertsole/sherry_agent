import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useAccessModeStore } from '../access-mode';

const bridge = vi.hoisted(() => ({
  fetchAccessMode: vi.fn(),
  setAccessMode: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

describe('stores/access-mode', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchAccessMode.mockReset();
    bridge.setAccessMode.mockReset();
  });

  it('defaults to auto-edit before hydration', () => {
    const store = useAccessModeStore();

    expect(store.modeFor('sid-1')).toBe('auto_edit');
  });

  it('hydrate() mirrors the mode the backend reports', async () => {
    const store = useAccessModeStore();
    bridge.fetchAccessMode.mockResolvedValueOnce('confirm_all');

    await store.hydrate('sid-1');

    expect(store.modeFor('sid-1')).toBe('confirm_all');
  });

  it('hydrate() keeps the last known mode when the read fails', async () => {
    const store = useAccessModeStore();
    bridge.fetchAccessMode.mockRejectedValueOnce(new Error('offline'));

    await store.hydrate('sid-1');

    expect(store.modeFor('sid-1')).toBe('auto_edit');
  });

  it('select() mirrors the value the backend applied', async () => {
    const store = useAccessModeStore();
    bridge.setAccessMode.mockResolvedValueOnce('confirm_all');

    await store.select('sid-1', 'confirm_all');

    expect(bridge.setAccessMode).toHaveBeenCalledWith('sid-1', 'confirm_all');
    expect(store.modeFor('sid-1')).toBe('confirm_all');
  });

  it('select() rolls back when the write fails', async () => {
    const store = useAccessModeStore();
    bridge.setAccessMode.mockRejectedValueOnce(new Error('offline'));

    await store.select('sid-1', 'full_access');

    expect(store.modeFor('sid-1')).toBe('auto_edit');
  });

  it('markYolo() mirrors a bypass-all the card already applied', () => {
    const store = useAccessModeStore();

    store.markYolo('sid-1');

    expect(store.modeFor('sid-1')).toBe('full_access');
    // Local mirror only: answering the card is what sets the backend flag.
    expect(bridge.setAccessMode).not.toHaveBeenCalled();
  });

  it('markYolo() leaves other sessions alone', () => {
    const store = useAccessModeStore();

    store.markYolo('sid-1');

    expect(store.modeFor('sid-2')).toBe('auto_edit');
  });
});
