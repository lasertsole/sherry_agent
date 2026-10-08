import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { EnvConfigPayload } from '../env';

// `env.ts` calls `fetchApi` as a Nuxt auto-import: under the unimport
// injection (vitest.config.ts) the binding resolves through the
// `../requestApi` module, so mock that module — a globalThis stub would never
// be read by an import binding.

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

import { readEnvConfig, writeEnvConfig } from '../env';

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
