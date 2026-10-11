import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { EnvConfigPayload } from '../env';

// `env.ts` calls `fetchApi` as a Nuxt auto-import: under the unimport
// injection (vitest.config.ts) the binding resolves through the
// `../requestApi` module, so mock that module — a globalThis stub would never
// be read by an import binding.

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

import {
  readEnvConfig,
  writeEnvConfig,
  readModelsConfig,
  writeModelsConfig,
  readNetworkConfig,
  writeNetworkConfig
} from '../env';

const payload: EnvConfigPayload = {
  groups: [
    {
      name: 'APP',
      entries: [
        { key: 'APP_NAME', value: 'EMA', value_edited: false },
        { key: 'APP_ENV', value: 'prod', value_edited: true }
      ]
    }
  ]
};

beforeEach(() => {
  fetchApiMock.mockReset();
});

describe('readEnvConfig', () => {
  it('fetches GET /env with a cache-busting timestamp and returns the group payload', async () => {
    fetchApiMock.mockResolvedValue(payload);

    await expect(readEnvConfig()).resolves.toEqual(payload);

    expect(fetchApiMock).toHaveBeenCalledTimes(1);
    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({
      url: '/env',
      method: 'get',
      opts: { _ts: expect.any(Number) }
    });
  });

  it('coerces an empty response body into the { groups: [] } shape', async () => {
    fetchApiMock.mockResolvedValue(null);

    await expect(readEnvConfig()).resolves.toEqual({ groups: [] });
  });

  it('propagates request errors so callers can distinguish network failure from an empty env file', async () => {
    fetchApiMock.mockRejectedValue(new Error('network down'));

    await expect(readEnvConfig()).rejects.toThrow('network down');
  });
});

describe('writeEnvConfig', () => {
  it('PUTs the changed keys and resolves true on success', async () => {
    fetchApiMock.mockResolvedValue({ code: 200 });

    await expect(writeEnvConfig({ APP_NAME: 'EMA2' })).resolves.toBe(true);

    expect(fetchApiMock).toHaveBeenCalledTimes(1);
    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({
      url: '/env',
      method: 'put',
      opts: { changes: { APP_NAME: 'EMA2' } }
    });
  });

  it('resolves false instead of throwing when the PUT request fails', async () => {
    fetchApiMock.mockRejectedValue(new Error('503'));

    await expect(writeEnvConfig({ APP_NAME: 'EMA2' })).resolves.toBe(false);
  });
});

describe('readModelsConfig / readNetworkConfig', () => {
  it('reads GET /models-config (the .models file) with a cache-busting timestamp', async () => {
    fetchApiMock.mockResolvedValue(payload);

    await expect(readModelsConfig()).resolves.toEqual(payload);

    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({
      url: '/models-config',
      method: 'get',
      opts: { _ts: expect.any(Number) }
    });
  });

  it('reads GET /network-config (the .network file)', async () => {
    fetchApiMock.mockResolvedValue({ groups: [] });

    await expect(readNetworkConfig()).resolves.toEqual({ groups: [] });

    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({ url: '/network-config', method: 'get' });
  });

  it('coerces an empty body into the { groups: [] } shape', async () => {
    fetchApiMock.mockResolvedValue(null);

    await expect(readModelsConfig()).resolves.toEqual({ groups: [] });
    await expect(readNetworkConfig()).resolves.toEqual({ groups: [] });
  });
});

describe('writeModelsConfig / writeNetworkConfig', () => {
  it('PUTs the changed .models keys (model families)', async () => {
    fetchApiMock.mockResolvedValue({ success: true });

    await expect(writeModelsConfig({ MAIN_LLM_NAME: 'deepseek-flash' })).resolves.toBe(true);

    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({
      url: '/models-config',
      method: 'put',
      opts: { changes: { MAIN_LLM_NAME: 'deepseek-flash' } }
    });
  });

  it('PUTs the changed .network keys (Tavily / SearXNG land in .network, never .env)', async () => {
    fetchApiMock.mockResolvedValue({ success: true });

    await expect(writeNetworkConfig({ SEARXNG_URL: 'http://localhost:8888' })).resolves.toBe(true);

    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({
      url: '/network-config',
      method: 'put',
      opts: { changes: { SEARXNG_URL: 'http://localhost:8888' } }
    });
  });

  it('resolves false instead of throwing when either PUT fails', async () => {
    fetchApiMock.mockRejectedValue(new Error('503'));

    await expect(writeModelsConfig({ MAIN_LLM_NAME: 'x' })).resolves.toBe(false);
    await expect(writeNetworkConfig({ TAVILY_API_KEY: 'k' })).resolves.toBe(false);
  });
});
