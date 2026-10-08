import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchModelConfig, getModelConfigCached, invalidateModelConfigCache, type ModelConfig } from '../model-config';

// `modelConfig.ts` calls `fetchApi` as a Nuxt auto-import: under the unimport
// injection (vitest.config.ts) the binding resolves through the `../requestApi`
// module, so mock that module — a globalThis stub would never be read by an
// import binding.

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

const validPayload: ModelConfig = {
  main_max_token: 131072,
  aux_max_token: 131072,
  min_required: 131072,
  valid: true
};

beforeEach(() => {
  fetchApiMock.mockReset();
  invalidateModelConfigCache();
});

describe('fetchModelConfig', () => {
  it('fetches GET /model-config with a cache-busting timestamp and returns the payload', async () => {
    fetchApiMock.mockResolvedValue(validPayload);

    await expect(fetchModelConfig()).resolves.toEqual(validPayload);

    expect(fetchApiMock).toHaveBeenCalledTimes(1);
    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({
      url: '/model-config',
      method: 'get',
      opts: { _ts: expect.any(Number) }
    });
  });

  it('coerces an empty response body into a default-invalid config', async () => {
    fetchApiMock.mockResolvedValue(null);

    const result = await fetchModelConfig();

    expect(result).toEqual({
      main_max_token: null,
      aux_max_token: null,
      min_required: 131072,
      valid: false
    });
  });
});

describe('getModelConfigCached', () => {
  it('fetches once and serves the cached config on subsequent calls', async () => {
    fetchApiMock.mockResolvedValue(validPayload);

    await expect(getModelConfigCached()).resolves.toEqual(validPayload);
    await expect(getModelConfigCached()).resolves.toEqual(validPayload);

    expect(fetchApiMock).toHaveBeenCalledTimes(1);
  });

  it('refetches after invalidateModelConfigCache', async () => {
    fetchApiMock.mockResolvedValue(validPayload);
    await getModelConfigCached();
    expect(fetchApiMock).toHaveBeenCalledTimes(1);

    invalidateModelConfigCache();
    await getModelConfigCached();

    expect(fetchApiMock).toHaveBeenCalledTimes(2);
  });

  it('resolves a default-invalid config instead of throwing when the request fails', async () => {
    fetchApiMock.mockRejectedValue(new Error('network down'));

    const result = await getModelConfigCached();

    expect(result.valid).toBe(false);
    expect(result.main_max_token).toBeNull();
  });
});
