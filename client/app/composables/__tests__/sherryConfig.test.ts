import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { SherryConfigPayload } from '../sherryConfig';

// `sherryConfig.ts` calls `fetchApi` as a Nuxt auto-import: the binding
// resolves through the `../requestApi` module (see unimport injection in
// vitest.config.ts), so mock that module (mirrors env.test.ts).

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.mock('../requestApi', () => ({ fetchApi: fetchApiMock }));

import { readSherryConfig, writeSherryConfig } from '../sherryConfig';

const payload: SherryConfigPayload = {
  entries: [
    { key: 'LOG_LEVEL', value: 'INFO', value_edited: false },
    { key: 'TAVILY_API_KEY', value: '', value_edited: true }
  ]
};

beforeEach(() => {
  fetchApiMock.mockReset();
});

describe('readSherryConfig', () => {
  it('fetches GET /sherry-config with a cache-busting timestamp and returns the entry payload', async () => {
    fetchApiMock.mockResolvedValue(payload);

    await expect(readSherryConfig()).resolves.toEqual(payload);

    expect(fetchApiMock).toHaveBeenCalledTimes(1);
    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({ url: '/sherry-config', method: 'get' });
    expect((call as { opts: Record<string, unknown> }).opts._ts).toBeTypeOf('number');
  });

  it('throws on a failed request instead of reporting an empty config', async () => {
    // fetchApi answers null for a network/HTTP failure; that must not render as
    // "no sherry.jsonc file found" (the packaged app's 500/503 case).
    fetchApiMock.mockResolvedValue(null);

    await expect(readSherryConfig()).rejects.toThrow('GET /sherry-config failed');
  });
});

describe('writeSherryConfig', () => {
  it('PUTs the changes payload to /sherry-config and resolves true on success', async () => {
    fetchApiMock.mockResolvedValue({ success: true });

    await expect(writeSherryConfig({ LOG_LEVEL: 'DEBUG' })).resolves.toBe(true);

    expect(fetchApiMock).toHaveBeenCalledTimes(1);
    const [call] = fetchApiMock.mock.calls[0] as [unknown];
    expect(call).toMatchObject({ url: '/sherry-config', method: 'put' });
    expect((call as { opts: Record<string, unknown> }).opts.changes).toEqual({ LOG_LEVEL: 'DEBUG' });
  });

  it('resolves false when the request fails', async () => {
    fetchApiMock.mockRejectedValue(new Error('boom'));

    await expect(writeSherryConfig({ LOG_LEVEL: 'DEBUG' })).resolves.toBe(false);
  });
});
