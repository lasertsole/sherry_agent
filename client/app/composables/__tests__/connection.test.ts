import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { toastInfo, toastWarn } from '~/composables/toast';
import { emit } from '../mitt';
import {
  isOnline,
  backendStatus,
  checkConnectivity,
  startConnectionWatch,
  stopConnectionWatch,
  useConnection,
  _setClientFlag,
  _resetStateForTest
} from '../connection';

/**
 * WS singleton mock: mutable state via vi.hoisted + getters simulates the /sessions/ws
 * singleton's connection state, letting test cases freely orchestrate
 * "connected / disconnected / readyState" without a real connection.
 * The mitt event bus uses the real implementation (connection subscribes and test cases
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

describe('connection 连通性监控（事件驱动）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    simulateWsClosed();
    _setClientFlag(true);
    // Reset module-level singleton private state (incl. lastReachable, which determines toast edge detection) and subscriptions
    _resetStateForTest();
  });

  afterEach(() => {
    stopConnectionWatch();
    vi.unstubAllGlobals();
    _setClientFlag(false);
    vi.restoreAllMocks();
  });

  it('初始 unknown -> ws:connected -> "ok"，且首连不弹恢复 toast', () => {
    expect(backendStatus.value).toBe('unknown');

    startConnectionWatch();
    emit('ws:connected', undefined);

    expect(backendStatus.value).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('connected 后 ws:disconnected -> "down" + backendDown warn toast', () => {
    startConnectionWatch();
    emit('ws:connected', undefined);
    expect(backendStatus.value).toBe('ok');

    emit('ws:disconnected', undefined);

    expect(backendStatus.value).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledTimes(1);
    // The test env has no Nuxt i18n, so safeT returns the key as-is (in production it would be the translated text)
    expect(mockToastWarn).toHaveBeenCalledWith('connection.backendDown');
    // Browser online: isOnline is unaffected
    expect(isOnline.value).toBe(true);
  });

  it('重连循环内重复 ws:disconnected 不重复弹 toast（边沿去重）', () => {
    startConnectionWatch();
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
    startConnectionWatch();
    emit('ws:connected', undefined);
    emit('ws:disconnected', undefined);
    expect(backendStatus.value).toBe('down');

    emit('ws:connected', undefined);

    expect(backendStatus.value).toBe('ok');
    expect(mockToastInfo).toHaveBeenCalledTimes(1);
    expect(mockToastInfo).toHaveBeenCalledWith('connection.backOnline');

    // Duplicate connected after recovery (no intermediate disconnect, e.g. reconnect race): no re-toast
    emit('ws:connected', undefined);
    expect(mockToastInfo).toHaveBeenCalledTimes(1);
  });

  it('window offline 事件 -> isOnline=false + offline toast；online 不乐观标记 ok', () => {
    startConnectionWatch();
    emit('ws:connected', undefined);
    expect(backendStatus.value).toBe('ok');

    vi.stubGlobal('navigator', { onLine: false });
    window.dispatchEvent(new Event('offline'));

    expect(isOnline.value).toBe(false);
    expect(backendStatus.value).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledTimes(1);
    expect(mockToastWarn).toHaveBeenCalledWith('connection.offline');

    // Back online: only isOnline is synced; backend ok is left to the WS reconnect event to decide
    vi.stubGlobal('navigator', { onLine: true });
    window.dispatchEvent(new Event('online'));

    expect(isOnline.value).toBe(true);
    expect(backendStatus.value).toBe('down');
    expect(mockToastInfo).not.toHaveBeenCalled();
  });

  it('start 时单例已 OPEN -> 立即 "ok"，无需任何事件', () => {
    simulateWsOpen();

    startConnectionWatch();

    expect(mockUseWs).toHaveBeenCalledTimes(1);
    expect(backendStatus.value).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('meta.client=false 时 startConnectionWatch 返回空操作句柄、不触碰单例', () => {
    _setClientFlag(false);

    const stop = startConnectionWatch();

    expect(typeof stop).toBe('function');
    expect(mockUseWs).not.toHaveBeenCalled();
    // Not subscribed: incoming events cannot change the state
    emit('ws:connected', undefined);
    expect(backendStatus.value).toBe('unknown');
    expect(() => stop()).not.toThrow();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('stopConnectionWatch 解除订阅：后续事件不再影响状态', () => {
    startConnectionWatch();
    stopConnectionWatch();

    simulateWsOpen();
    emit('ws:connected', undefined);
    emit('ws:disconnected', undefined);

    expect(backendStatus.value).toBe('unknown');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('checkConnectivity 手动同步 readyState 且不弹 toast（无网络请求）', async () => {
    startConnectionWatch();

    simulateWsOpen();
    await checkConnectivity();
    expect(backendStatus.value).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();

    simulateWsClosed();
    await checkConnectivity();
    expect(backendStatus.value).toBe('down');
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('重复 start 幂等：不重复初始化单例、不叠加订阅', () => {
    startConnectionWatch();
    startConnectionWatch();

    expect(mockUseWs).toHaveBeenCalledTimes(1);
  });

  it('useConnection 返回共享单例状态与同一组控制函数', () => {
    const c = useConnection();
    expect(c.isOnline).toBe(isOnline);
    expect(c.backendStatus).toBe(backendStatus);
    expect(c.startConnectionWatch).toBe(startConnectionWatch);
    expect(c.stopConnectionWatch).toBe(stopConnectionWatch);
    expect(c.checkConnectivity).toBe(checkConnectivity);
  });
});
