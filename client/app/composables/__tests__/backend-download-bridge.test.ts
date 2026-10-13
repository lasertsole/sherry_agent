import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The backend-download bridge calls: what the client half does in each runtime,
 * and that the event subscription really detaches every listener. The Tauri
 * imports are lazy in the transport layer, so the seam mocked here is
 * `./transport` itself.
 */
const transport = {
  isTauri: vi.fn(() => true),
  getListen: vi.fn(),
  invokeNative: vi.fn()
};

vi.mock('../bridge/transport', () => transport);

/**
 * A `listen` stand-in that records the handlers per event name.
 * @returns The handler table plus the detach spy.
 */
function fakeListen() {
  const handlers = new Map<string, (event: unknown) => void>();
  const detach = vi.fn();
  transport.getListen.mockResolvedValue((eventName: string, handler: (e: unknown) => void) => {
    handlers.set(eventName, handler);
    return detach;
  });
  return { handlers, detach };
}

async function loadBridge() {
  vi.resetModules();
  return import('../bridge/backend-download');
}

beforeEach(() => {
  transport.isTauri.mockReset().mockReturnValue(true);
  transport.getListen.mockReset();
  transport.invokeNative.mockReset().mockResolvedValue({ kind: 'native', value: null });
});

describe('backend-download bridge', () => {
  it('pulls the status snapshot through IPC', async () => {
    transport.invokeNative.mockResolvedValue({
      kind: 'native',
      value: { state: 'running', downloaded: 1, total: 2, percent: 50, error: null }
    });
    const bridge = await loadBridge();

    const status = await bridge.fetchBackendDownloadStatus();

    expect(transport.invokeNative).toHaveBeenCalledWith('backend_download_status');
    expect(status?.state).toBe('running');
  });

  it('answers null outside the desktop shell', async () => {
    transport.invokeNative.mockResolvedValue(null);
    const bridge = await loadBridge();

    expect(await bridge.fetchBackendDownloadStatus()).toBeNull();
    expect(await bridge.retryBackendDownload()).toBe(false);
    expect(transport.getListen).not.toHaveBeenCalled();
  });

  it('wires all four download events to their handlers', async () => {
    const { handlers, detach } = fakeListen();
    const bridge = await loadBridge();
    const sink = {
      onStarted: vi.fn(),
      onProgress: vi.fn(),
      onComplete: vi.fn(),
      onError: vi.fn()
    };

    const unsubscribe = await bridge.subscribeBackendDownload(sink);

    expect([...handlers.keys()].sort()).toEqual([
      'backend-download-complete',
      'backend-download-error',
      'backend-download-progress',
      'backend-download-started'
    ]);
    handlers.get('backend-download-started')!({});
    expect(sink.onStarted).toHaveBeenCalledTimes(1);

    handlers.get('backend-download-progress')!({ payload: { downloaded: 5, total: 10, percent: 50 } });
    expect(sink.onProgress).toHaveBeenCalledWith({ downloaded: 5, total: 10, percent: 50 });

    handlers.get('backend-download-complete')!({});
    expect(sink.onComplete).toHaveBeenCalledTimes(1);

    handlers.get('backend-download-error')!({ payload: { error: 'HTTP 404' } });
    expect(sink.onError).toHaveBeenCalledWith('HTTP 404');

    unsubscribe();
    expect(detach).toHaveBeenCalledTimes(4);
  });

  it('reports a failure that arrived without a message', async () => {
    const { handlers } = fakeListen();
    const bridge = await loadBridge();
    const onError = vi.fn();

    await bridge.subscribeBackendDownload({
      onStarted: vi.fn(),
      onProgress: vi.fn(),
      onComplete: vi.fn(),
      onError
    });
    handlers.get('backend-download-error')!({});

    expect(onError).toHaveBeenCalledWith('backend download failed');
  });

  it('subscribes to nothing outside the desktop shell', async () => {
    transport.isTauri.mockReturnValue(false);
    const bridge = await loadBridge();

    const unsubscribe = await bridge.subscribeBackendDownload({
      onStarted: vi.fn(),
      onProgress: vi.fn(),
      onComplete: vi.fn(),
      onError: vi.fn()
    });

    expect(transport.getListen).not.toHaveBeenCalled();
    expect(() => unsubscribe()).not.toThrow();
  });

  it('asks the shell to retry through the command', async () => {
    transport.invokeNative.mockResolvedValue({ kind: 'native', value: null });
    const bridge = await loadBridge();

    expect(await bridge.retryBackendDownload()).toBe(true);
    expect(transport.invokeNative).toHaveBeenCalledWith('retry_backend_download');
  });

  it("propagates the shell's refusal message", async () => {
    transport.invokeNative.mockRejectedValue('download failed: HTTP 404');
    const bridge = await loadBridge();

    await expect(bridge.retryBackendDownload()).rejects.toBe('download failed: HTTP 404');
  });
});
