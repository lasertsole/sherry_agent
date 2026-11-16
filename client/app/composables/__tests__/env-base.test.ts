import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('API base URL', () => {
  it('same-origin keeps REST relative and derives WS from the page origin', async () => {
    vi.stubEnv('VITE_API_BACK_URL', 'same-origin');

    const env = await import('../env');

    expect(env.API_BASE_URL).toBe('');
    expect(env.WS_BASE_URL).toMatch(/^wss?:\/\//);
  });

  it('keeps the historical localhost fallback when unset', async () => {
    vi.stubEnv('VITE_API_BACK_URL', '');

    const env = await import('../env');

    expect(env.API_BASE_URL).toBe('http://localhost:8080');
    expect(env.WS_BASE_URL).toBe('ws://localhost:8080');
  });
});
