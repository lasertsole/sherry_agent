import { beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import type { BackendDownloadHandlers } from '../bridge/backend-download';
import type { BackendDownloadStatus } from '~/types/backend/backend-download-status';

/**
 * The first-run download state has TWO sources of truth, and the order between
 * them is the whole point: the shell starts downloading before the SPA mounts,
 * so the state is pulled once over IPC and the events only take over from there.
 * These tests drive both halves through a mocked bridge.
 */
const bridge = {
  fetchBackendDownloadStatus: vi.fn(),
  subscribeBackendDownload: vi.fn(),
  retryBackendDownload: vi.fn()
};

vi.mock('~/composables/bridge', () => bridge);

const snapshot = (over: Partial<BackendDownloadStatus> = {}): BackendDownloadStatus => ({
  state: 'idle',
  downloaded: 0,
  total: 0,
  percent: 0,
  error: null,
  ...over
});

/**
 * Import a FRESH composable module (its state lives at module scope).
 * @returns The freshly imported module.
 */
async function freshModule() {
  vi.resetModules();
  return import('../use-backend-download');
}

/**
 * Capture the handlers the composable subscribed with.
 * @returns The handlers of the latest subscription.
 */
function capturedHandlers(): BackendDownloadHandlers {
  const [handlers] = bridge.subscribeBackendDownload.mock.calls.at(-1) as [BackendDownloadHandlers];
  return handlers;
}

beforeEach(() => {
  bridge.fetchBackendDownloadStatus.mockReset().mockResolvedValue(null);
  bridge.subscribeBackendDownload.mockReset().mockResolvedValue(() => {});
  bridge.retryBackendDownload.mockReset().mockResolvedValue(true);
});

describe('useBackendDownload', () => {
  it("pulls the shell's current state before the events take over", async () => {
    // The shell had already started downloading when the SPA mounted.
    bridge.fetchBackendDownloadStatus.mockResolvedValue(
      snapshot({ state: 'running', downloaded: 512, total: 1024, percent: 50 })
    );
    const { initBackendDownload, useBackendDownload } = await freshModule();

    await initBackendDownload();

    const state = useBackendDownload();
    expect(state.state.value).toBe('running');
    expect(state.percent.value).toBe(50);
    expect(state.downloadedBytes.value).toBe(512);
    expect(state.totalBytes.value).toBe(1024);
    expect(state.error.value).toBe('');
  });

  it('treats a snapshot that already reads "ready" as nothing to do', async () => {
    // The shell keeps `ready` after a successful launch. A page that mounts then
    // (the reload after the hand-off, or any later mount) must NOT show the card
    // again — it would re-arm the reload and loop.
    bridge.fetchBackendDownloadStatus.mockResolvedValue(snapshot({ state: 'ready', percent: 100 }));
    const { initBackendDownload, useBackendDownload } = await freshModule();

    await initBackendDownload();

    expect(useBackendDownload().state.value).toBe('idle');
  });

  it('stays idle outside the desktop shell (the pull answers null)', async () => {
    const { initBackendDownload, useBackendDownload } = await freshModule();

    await initBackendDownload();

    expect(useBackendDownload().state.value).toBe('idle');
  });

  it("mirrors the shell's events once subscribed", async () => {
    const { initBackendDownload, useBackendDownload } = await freshModule();
    await initBackendDownload();
    const handlers = capturedHandlers();
    const state = useBackendDownload();

    handlers.onStarted();
    expect(state.state.value).toBe('running');

    handlers.onProgress({ downloaded: 3 * 1024 * 1024, total: 6 * 1024 * 1024, percent: 50 });
    expect(state.state.value).toBe('running');
    expect(state.percent.value).toBe(50);
    expect(state.downloadedBytes.value).toBe(3 * 1024 * 1024);
    expect(state.totalBytes.value).toBe(6 * 1024 * 1024);

    handlers.onComplete();
    expect(state.state.value).toBe('ready');
    expect(state.percent.value).toBe(100);

    handlers.onError('HTTP 404');
    expect(state.state.value).toBe('failed');
    expect(state.error.value).toBe('HTTP 404');
  });

  it('init is idempotent — one pull and one subscription per app boot', async () => {
    const { initBackendDownload } = await freshModule();

    await initBackendDownload();
    await initBackendDownload();

    expect(bridge.fetchBackendDownloadStatus).toHaveBeenCalledTimes(1);
    expect(bridge.subscribeBackendDownload).toHaveBeenCalledTimes(1);
  });

  it('retry asks the shell again, and a refusal keeps the message on screen', async () => {
    const { initBackendDownload, useBackendDownload } = await freshModule();
    await initBackendDownload();
    const state = useBackendDownload();
    state.state.value = 'failed';
    state.error.value = 'HTTP 404';

    await state.retry();
    expect(bridge.retryBackendDownload).toHaveBeenCalledTimes(1);
    expect(state.state.value).toBe('running');
    expect(state.error.value).toBe('');

    bridge.retryBackendDownload.mockRejectedValue('no network');
    await state.retry();
    expect(state.state.value).toBe('failed');
    expect(state.error.value).toBe('no network');
  });

  it('a failed subscription leaves the pulled snapshot in place', async () => {
    // An older shell may lack the event channel; the pull still answered.
    bridge.fetchBackendDownloadStatus.mockResolvedValue(snapshot({ state: 'running', percent: 10 }));
    bridge.subscribeBackendDownload.mockRejectedValue(new Error('no channel'));
    const { initBackendDownload, useBackendDownload } = await freshModule();

    await initBackendDownload();
    await nextTick();

    expect(useBackendDownload().state.value).toBe('running');
  });
});
