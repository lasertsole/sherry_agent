import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';

const runtime = vi.hoisted(() => ({
  resolveRuntimeT: vi.fn((): ((key: string) => string) | undefined => undefined)
}));

vi.mock('@/composables/i18nRuntime', () => ({ resolveRuntimeT: runtime.resolveRuntimeT }));

// The shared Vitest setup imports `safeT` (through the v-safe-html directive)
// before this file's mock is installed. A fresh module graph after the mock
// makes the module under test observe the stub, without relying on import order.
let safeT: (key: string) => string;
let _setClientFlag: (client: boolean) => void;

beforeAll(async () => {
  vi.resetModules();
  const i18n = await import('../i18n');
  const client = await import('../client');
  safeT = i18n.safeT;
  _setClientFlag = client._setClientFlag;
});

describe('safeT', () => {
  afterEach(() => {
    _setClientFlag(false);
    runtime.resolveRuntimeT.mockReset();
    runtime.resolveRuntimeT.mockReturnValue(undefined);
  });

  it('returns the key unchanged when not on the client and never resolves a translator', () => {
    _setClientFlag(false);
    expect(safeT('connection.offline')).toBe('connection.offline');
    expect(runtime.resolveRuntimeT).not.toHaveBeenCalled();
  });

  it('returns the key unchanged when no runtime translator is available', () => {
    _setClientFlag(true);
    expect(safeT('connection.offline')).toBe('connection.offline');
    expect(runtime.resolveRuntimeT).toHaveBeenCalledTimes(1);
  });

  it('delegates to the resolved runtime translator on the client', () => {
    _setClientFlag(true);
    runtime.resolveRuntimeT.mockReturnValue((key: string) => `zh:${key}`);
    expect(safeT('connection.offline')).toBe('zh:connection.offline');
  });
});
