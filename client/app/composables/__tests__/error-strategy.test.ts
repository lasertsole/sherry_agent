import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// The strategy forbids non-boundary sites from raising user-visible toasts, so
// every toast helper is spied at the module boundary and asserted untouched.
const toastSpies = vi.hoisted(() => ({
  sendRequestErrorToast: vi.fn(),
  toastError: vi.fn(),
  toastWarn: vi.fn(),
  toastInfo: vi.fn(),
  toastSuccess: vi.fn(),
  registerToastApi: vi.fn()
}));
vi.mock('../toast', () => toastSpies);

// Never touch a real IndexedDB instance (messages.ts imports db at module load).
const dbMock = vi.hoisted(() => ({
  cacheMessages: vi.fn(async () => {}),
  readCachedMessages: vi.fn(async () => []),
  cachedMaxTurnNum: vi.fn(async () => 0),
  clearCachedSession: vi.fn(async () => {})
}));
vi.mock('../db', () => dbMock);

const apiMock = vi.hoisted(() => ({
  fetchApi: vi.fn(),
  fetchApiRaw: vi.fn(),
  fetchApiPayload: vi.fn()
}));
vi.mock('../requestApi', () => apiMock);

import { checkHealth } from '../bridge/health';
import { uploadBase64ToUrls } from '../bridge/upload';
import { clearSession } from '../messages';
import { createWsMessageHandler } from '../ws-message';

/** Every toast helper must stay untouched by the non-boundary sites under test. */
const expectNoToasts = () => {
  for (const spy of Object.values(toastSpies)) expect(spy).not.toHaveBeenCalled();
};

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('error-handling strategy: health probe stays silent', () => {
  it('maps an HTTP failure to a default object (no toast, single probe)', async () => {
    apiMock.fetchApiRaw.mockResolvedValue({ ok: false, status: 503 });
    await expect(checkHealth()).resolves.toEqual({ healthy: false, message: 'HTTP 503' });
    expect(apiMock.fetchApiRaw).toHaveBeenCalledTimes(1);
    expectNoToasts();
  });

  it('maps a transport failure to String(error) (no toast, no retry)', async () => {
    apiMock.fetchApiRaw.mockRejectedValue(new Error('net down'));
    await expect(checkHealth()).resolves.toEqual({ healthy: false, message: 'Error: net down' });
    expect(apiMock.fetchApiRaw).toHaveBeenCalledTimes(1);
    expectNoToasts();
  });

  it('reports reachable on success without toasting', async () => {
    apiMock.fetchApiRaw.mockResolvedValue({ ok: true, status: 200 });
    await expect(checkHealth()).resolves.toEqual({ healthy: true, message: 'Python backend reachable' });
    expectNoToasts();
  });
});

describe('error-handling strategy: upload owns its failure contract', () => {
  it('throws the labelled network error and never toasts (single attempt)', async () => {
    apiMock.fetchApiRaw.mockRejectedValue(new Error('net down'));
    await expect(uploadBase64ToUrls('image', ['AAAA'])).rejects.toThrow('Image upload network error: Error: net down');
    expect(apiMock.fetchApiRaw).toHaveBeenCalledTimes(1);
    expectNoToasts();
  });

  it('throws the labelled HTTP error and never toasts (no retry)', async () => {
    apiMock.fetchApiRaw.mockResolvedValue({ ok: false, status: 500 });
    await expect(uploadBase64ToUrls('audio', ['AAAA'])).rejects.toThrow('Audio upload failed: HTTP 500');
    expect(apiMock.fetchApiRaw).toHaveBeenCalledTimes(1);
    expectNoToasts();
  });
});

describe('error-handling strategy: domain fallbacks stay silent', () => {
  it('clearSession swallows a failure as false without toasting', async () => {
    apiMock.fetchApi.mockRejectedValue(new Error('down'));
    await expect(clearSession('s1')).resolves.toBe(false);
    expectNoToasts();
  });

  it('a malformed WS frame resolves null without toasting', () => {
    const handler = createWsMessageHandler({});
    expect(handler({ data: 'not json' } as MessageEvent)).toBeNull();
    expectNoToasts();
  });
});
