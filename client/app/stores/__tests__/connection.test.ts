import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { toastInfo, toastWarn } from '~/composables/toast';
import { emit } from '~/composables/mitt';
import { _setClientFlag } from '~/utils/client';
import { useConnectionStore } from '../connection';

/**
 * WS singleton mock: mutable state via vi.hoisted + getters simulates the /sessions/ws
 * singleton's connection state, letting test cases freely orchestrate
 * "connected / disconnected / readyState" without a real connection.
 * The mitt event bus uses the real implementation (the store subscribes and test cases
 * trigger via emit — the most realistic chain).
 */
const wsState = vi.hoisted(() => ({
  /** isConnected.value */
  connected: false,
  /** Simulates the singleton socket's readyState (1=OPEN, 3=CLOSED); when CLOSED, ws.value is treated as null */
  readyState: 3
}));

vi.mock('~/composables/ws', () => ({
  useWs: vi.fn(() => ({
    // getter reads state dynamically: startConnectionWatch's initial convergence can read the orchestrated value
    ws: {
      get value(): { readyState: number } | null {
        return wsState.readyState === 3 ? null : { readyState: wsState.readyState };
      }
    },
    isConnected: {
      get value(): boolean {
        return wsState.connected;
      }
    }
  })),
  closeWs: vi.fn(),
  isSessionWsOpen: vi.fn(() => wsState.readyState === 1)
}));
vi.mock('~/composables/toast', () => ({
  registerToastApi: vi.fn(),
  toastInfo: vi.fn(),
  toastSuccess: vi.fn(),
  toastWarn: vi.fn(),
  toastError: vi.fn(),
  sendRequestErrorToast: vi.fn()
}));

import { useWs } from '~/composables/ws';

const mockUseWs = vi.mocked(useWs);
const mockToastInfo = vi.mocked(toastInfo);
const mockToastWarn = vi.mocked(toastWarn);

let store: ReturnType<typeof useConnectionStore>;

/** Orchestration: singleton socket established (OPEN + isConnected), for initial convergence / readyState sync */
function simulateWsOpen(): void {
  wsState.connected = true;
  wsState.readyState = 1; // WebSocket.OPEN
}

/** Orchestration: singleton socket disconnected (CLOSED + isConnected=false, ws.value is null) */
function simulateWsClosed(): void {
  wsState.connected = false;
  wsState.readyState = 3; // WebSocket.CLOSED
}

describe('stores/connection 连通性监控（事件驱动）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    simulateWsClosed();
    _setClientFlag(true);
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useConnectionStore();
    // Reset store private state (incl. lastReachable, which determines toast edge detection) and subscriptions
    store._resetStateForTest();
  });

  afterEach(() => {
    store.stopConnectionWatch();
    vi.unstubAllGlobals();
    _setClientFlag(false);
    vi.restoreAllMocks();
  });

  it('返回同一单例实例', () => {
    expect(useConnectionStore()).toBe(store);
    expect(store.isOnline).toBe(true);
    expect(store.backendStatus).toBe('unknown');
  });

  it('初始 unknown -> ws:connected -> "ok"，且首连不弹恢复 toast', () => {
    expect(store.backendStatus).toBe('unknown');

    store.startConnectionWatch();
    emit('ws:connected', undefined);

    expect(store.backendStatus).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('connected 后 ws:disconnected -> "down" + backendDown warn toast', () => {
    store.startConnectionWatch();
    emit('ws:connected', undefined);
    expect(store.backendStatus).toBe('ok');

    emit('ws:disconnected', undefined);

    expect(store.backendStatus).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledTimes(1);
    // The test env has no Nuxt i18n, so safeT returns the key as-is (in production it would be the translated text)
    expect(mockToastWarn).toHaveBeenCalledWith('connection.backendDown');
    // Browser online: isOnline is unaffected
    expect(store.isOnline).toBe(true);
  });

  it('重连循环内重复 ws:disconnected 不重复弹 toast（边沿去重）', () => {
    store.startConnectionWatch();
    emit('ws:connected', undefined);
    emit('ws:disconnected', undefined);
    expect(mockToastWarn).toHaveBeenCalledTimes(1);

    // While the backend is down the reconnect loop disconnects every 5s: must never re-toast repeatedly
    emit('ws:disconnected', undefined);
    emit('ws:disconnected', undefined);

    expect(mockToastWarn).toHaveBeenCalledTimes(1);
    expect(mockToastInfo).not.toHaveBeenCalled();
  });

  it('down -> ws:connected 恢复时弹 backOnline info toast（同边沿去重）', () => {
    store.startConnectionWatch();
    emit('ws:connected', undefined);
    emit('ws:disconnected', undefined);
    expect(store.backendStatus).toBe('down');

    emit('ws:connected', undefined);

    expect(store.backendStatus).toBe('ok');
    expect(mockToastInfo).toHaveBeenCalledTimes(1);
    expect(mockToastInfo).toHaveBeenCalledWith('connection.backOnline');

    // Duplicate connected after recovery (no intermediate disconnect, e.g. reconnect race): no re-toast
    emit('ws:connected', undefined);
    expect(mockToastInfo).toHaveBeenCalledTimes(1);
  });

  it('window offline 事件 -> isOnline=false + offline toast；online 不乐观标记 ok', () => {
    store.startConnectionWatch();
    emit('ws:connected', undefined);
    expect(store.backendStatus).toBe('ok');

    vi.stubGlobal('navigator', { onLine: false });
    window.dispatchEvent(new Event('offline'));

    expect(store.isOnline).toBe(false);
    expect(store.backendStatus).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledTimes(1);
    expect(mockToastWarn).toHaveBeenCalledWith('connection.offline');

    // Back online: only isOnline is synced; backend ok is left to the WS reconnect event to decide
    vi.stubGlobal('navigator', { onLine: true });
    window.dispatchEvent(new Event('online'));

    expect(store.isOnline).toBe(true);
    expect(store.backendStatus).toBe('down');
    expect(mockToastInfo).not.toHaveBeenCalled();
  });

  it('start 时单例已 OPEN -> 立即 "ok"，无需任何事件', () => {
    simulateWsOpen();

    store.startConnectionWatch();

    expect(mockUseWs).toHaveBeenCalledTimes(1);
    expect(store.backendStatus).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('meta.client=false 时 startConnectionWatch 返回空操作句柄、不触碰单例', () => {
    _setClientFlag(false);

    const stop = store.startConnectionWatch();

    expect(typeof stop).toBe('function');
    expect(mockUseWs).not.toHaveBeenCalled();
    // Not subscribed: incoming events cannot change the state
    emit('ws:connected', undefined);
    expect(store.backendStatus).toBe('unknown');
    expect(() => stop()).not.toThrow();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('stopConnectionWatch 解除订阅：后续事件不再影响状态', () => {
    store.startConnectionWatch();
    store.stopConnectionWatch();

    simulateWsOpen();
    emit('ws:connected', undefined);
    emit('ws:disconnected', undefined);

    expect(store.backendStatus).toBe('unknown');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('checkConnectivity 手动同步 readyState 且不弹 toast（无网络请求）', async () => {
    store.startConnectionWatch();

    simulateWsOpen();
    await store.checkConnectivity();
    expect(store.backendStatus).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();

    simulateWsClosed();
    await store.checkConnectivity();
    expect(store.backendStatus).toBe('down');
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('重复 start 幂等：不重复初始化单例、不叠加订阅', () => {
    store.startConnectionWatch();
    store.startConnectionWatch();

    expect(mockUseWs).toHaveBeenCalledTimes(1);
  });
});

describe('connection bannerState', () => {
  const mount = (isOnline: boolean, backendStatus: 'unknown' | 'ok' | 'down') => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    const store = useConnectionStore();
    store._resetStateForTest();
    store.isOnline = isOnline;
    store.backendStatus = backendStatus;
    return store;
  };

  it('hides the banner while the WS heartbeat confirms the backend (offline hint ignored)', () => {
    // Embedded webviews can report navigator.onLine === false while the app is
    // fully reachable; live evidence must win.
    expect(mount(false, 'ok').bannerState).toBe('hidden');
    expect(mount(true, 'ok').bannerState).toBe('hidden');
  });

  it('shows the red offline banner only without positive evidence', () => {
    expect(mount(false, 'down').bannerState).toBe('offline');
    expect(mount(false, 'unknown').bannerState).toBe('offline');
  });

  it('shows the amber backend banner when the browser is online but the backend is down', () => {
    expect(mount(true, 'down').bannerState).toBe('backend-down');
    expect(mount(true, 'unknown').bannerState).toBe('hidden');
  });
});
