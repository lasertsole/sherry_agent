import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { ENV_MODEL_ID, useSessionModelStore } from '../session-model';

const bridge = vi.hoisted(() => ({
  fetchSessionModel: vi.fn(),
  setSessionModel: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

const profile = {
  id: 'p1',
  label: 'Kimi K2',
  provider: 'openai',
  model: 'kimi-k2',
  base_url: 'https://api.moonshot.cn/v1',
  api_key: 'sk-kimi'
};

const envModel = { provider: 'zhipu', model: 'glm-4.6' };

describe('stores/session-model', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchSessionModel.mockReset();
    bridge.setSessionModel.mockReset();
  });

  it('defaults to following the env config before hydration', () => {
    const store = useSessionModelStore();
    expect(store.currentId('sid-1')).toBe(ENV_MODEL_ID);
    expect(store.overrideBySession['sid-1']).toBeUndefined();
  });

  it('hydrate() mirrors a stored override and the env identity', async () => {
    const store = useSessionModelStore();
    bridge.fetchSessionModel.mockResolvedValueOnce({
      override: { id: 'p1', label: 'Kimi K2', model: 'kimi-k2', has_api_key: true },
      env_model: envModel
    });

    await store.hydrate('sid-1');

    expect(store.currentId('sid-1')).toBe('p1');
    expect(store.envModel).toEqual(envModel);
    expect(store.overrideBySession['sid-1']?.model).toBe('kimi-k2');
  });

  it('hydrate() maps a null override onto the env entry', async () => {
    const store = useSessionModelStore();
    bridge.fetchSessionModel.mockResolvedValueOnce({ override: null, env_model: envModel });

    await store.hydrate('sid-2');

    expect(store.currentId('sid-2')).toBe(ENV_MODEL_ID);
    expect(store.overrideBySession['sid-2']).toBeNull();
  });

  it('hydrate() ignores empty session ids', async () => {
    const store = useSessionModelStore();
    await store.hydrate('');
    expect(bridge.fetchSessionModel).not.toHaveBeenCalled();
  });

  it('select() switches optimistically and mirrors the stored state', async () => {
    const store = useSessionModelStore();
    bridge.setSessionModel.mockResolvedValueOnce({
      override: { id: 'p1', label: 'Kimi K2', model: 'kimi-k2', has_api_key: true },
      env_model: envModel,
      pending: false
    });

    await store.select('sid-1', profile);

    expect(store.currentId('sid-1')).toBe('p1');
    expect(store.overrideBySession['sid-1']?.model).toBe('kimi-k2');
    expect(store.isPending('sid-1')).toBe(false);
    expect(bridge.setSessionModel).toHaveBeenCalledWith('sid-1', profile);
  });

  it('select() mid-turn mirrors the parked flag', async () => {
    const store = useSessionModelStore();
    bridge.setSessionModel.mockResolvedValueOnce({
      override: { id: 'p1', label: 'Kimi K2', model: 'kimi-k2', has_api_key: true },
      env_model: envModel,
      pending: true
    });

    await store.select('sid-1', profile);

    expect(store.currentId('sid-1')).toBe('p1');
    expect(store.isPending('sid-1')).toBe(true);
  });

  it('select(null) returns to the env entry', async () => {
    const store = useSessionModelStore();
    bridge.setSessionModel.mockResolvedValueOnce({
      override: null,
      env_model: envModel,
      pending: false
    });

    await store.select('sid-1', null);

    expect(store.currentId('sid-1')).toBe(ENV_MODEL_ID);
    expect(store.overrideBySession['sid-1']).toBeNull();
    expect(bridge.setSessionModel).toHaveBeenCalledWith('sid-1', null);
  });

  it('select() rolls back when the backend rejects the write', async () => {
    const store = useSessionModelStore();
    bridge.setSessionModel.mockRejectedValueOnce(new Error('busy'));

    await store.select('sid-1', profile);

    expect(store.currentId('sid-1')).toBe(ENV_MODEL_ID);
    expect(store.overrideBySession['sid-1']).toBeNull();
  });

  it('tracks sessions independently', async () => {
    const store = useSessionModelStore();
    bridge.fetchSessionModel.mockResolvedValueOnce({
      override: { id: 'p1', label: 'Kimi K2', model: 'kimi-k2', has_api_key: true },
      env_model: envModel
    });
    await store.hydrate('sid-a');

    expect(store.currentId('sid-a')).toBe('p1');
    expect(store.currentId('sid-b')).toBe(ENV_MODEL_ID);
  });
});
