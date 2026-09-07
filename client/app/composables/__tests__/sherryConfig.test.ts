import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { SherryConfigPayload } from '../sherryConfig';

// `sherryConfig.ts` calls `fetchApi` as a Nuxt auto-import (no explicit import
// in source). Stub the global with our own mock, mirroring env.test.ts.

const fetchApiMock = vi.hoisted(() => vi.fn());

vi.stubGlobal('fetchApi', fetchApiMock);

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

  it('falls back to an empty entry list on an empty body without throwing', async () => {
    fetchApiMock.mockResolvedValue(undefined);

    await expect(readSherryConfig()).resolves.toEqual({ entries: [] });
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
