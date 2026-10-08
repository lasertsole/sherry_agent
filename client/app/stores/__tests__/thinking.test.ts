import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useThinkingStore } from '../thinking';

const bridge = vi.hoisted(() => ({
  fetchThinkingState: vi.fn(),
  setThinkingValue: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

describe('stores/thinking', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchThinkingState.mockReset();
    bridge.setThinkingValue.mockReset();
  });

  it('defaults to on_off mode with switch off before hydration', () => {
    const store = useThinkingStore();
    expect(store.mode).toBe('on_off');
    expect(store.current('sid-1')).toBe(false);
  });

  it('hydrate() mirrors a boolean choice for on_off models', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'on_off',
      enabled: true,
      level: null
    });
    await store.hydrate('sid-on');
    expect(store.mode).toBe('on_off');
    expect(store.current('sid-on')).toBe(true);
  });

  it('hydrate() mirrors a level choice for always-think models', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'levels',
      enabled: null,
      level: 'low'
    });
    await store.hydrate('sid-lvl');
    expect(store.mode).toBe('levels');
    expect(store.current('sid-lvl')).toBe('low');
  });

  it('hydrate() maps null flags to the display defaults', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'levels',
      enabled: null,
      level: null
    });
    await store.hydrate('sid-null');
    // unset level displays as 高 (the server-side default level)
    expect(store.current('sid-null')).toBe('high');
  });

  it('hydrate() follows the reported env default for an unset switch', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'on_off',
      enabled: null,
      level: null,
      // MAIN_LLM_ENABLE_THINKING=true: the model still thinks, so the switch
      // must read 开启 even though the session never chose explicitly.
      defaultEnabled: true,
      defaultLevel: 'high'
    });
    await store.hydrate('sid-env');
    expect(store.current('sid-env')).toBe(true);
    // The default also covers sessions that have not been hydrated themselves.
    expect(store.current('sid-other')).toBe(true);
  });

  it('hydrate() follows the reported default level for an unset selector', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'levels',
      enabled: null,
      level: null,
      defaultEnabled: true,
      defaultLevel: 'low'
    });
    await store.hydrate('sid-lvl-default');
    expect(store.current('sid-lvl-default')).toBe('low');
  });

  it('hydrate() ignores empty session ids', async () => {
    const store = useThinkingStore();
    await store.hydrate('');
    expect(bridge.fetchThinkingState).not.toHaveBeenCalled();
  });

  it('setValue() persists and updates the control optimistically', async () => {
    const store = useThinkingStore();
    bridge.setThinkingValue.mockResolvedValueOnce({ pending: false });

    await store.setValue('sid-1', 'max');

    expect(store.current('sid-1')).toBe('max');
    expect(bridge.setThinkingValue).toHaveBeenCalledWith('sid-1', 'max');
    // The write landed live: nothing is parked.
    expect(store.isPending('sid-1')).toBe(false);
  });

  it('setValue() mid-turn mirrors the parked flag from the backend', async () => {
    const store = useThinkingStore();
    bridge.setThinkingValue.mockResolvedValueOnce({ pending: true });

    await store.setValue('sid-1', true);

    // The picked value shows immediately; it lands on the next turn.
    expect(store.current('sid-1')).toBe(true);
    expect(store.isPending('sid-1')).toBe(true);
  });

  it('hydrate() mirrors the parked flag', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'on_off',
      enabled: true,
      level: null,
      pending: true
    });

    await store.hydrate('sid-parked');

    expect(store.isPending('sid-parked')).toBe(true);
  });

  it('setValue() rolls back when the backend rejects the write', async () => {
    const store = useThinkingStore();
    bridge.setThinkingValue.mockRejectedValueOnce(new Error('offline'));

    await store.setValue('sid-1', true);

    expect(store.current('sid-1')).toBe(false);
    expect(store.isPending('sid-1')).toBe(false);
  });

  it('tracks sessions independently', async () => {
    const store = useThinkingStore();
    bridge.fetchThinkingState.mockResolvedValueOnce({
      mode: 'levels',
      enabled: null,
      level: 'low'
    });
    await store.hydrate('sid-a');

    expect(store.current('sid-a')).toBe('low');
    expect(store.current('sid-b')).toBe('high');
  });
});
