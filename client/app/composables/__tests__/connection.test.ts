import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { checkHealth } from '~/composables/bridge';
import { toastInfo, toastWarn } from '~/composables/toast';
import {
  isOnline,
  backendStatus,
  checkConnectivity,
  startConnectionWatch,
  stopConnectionWatch,
  useConnection,
  _setClientFlag,
  _resetStateForTest,
} from '../connection';

// checkHealth 是 bridge.ts 的网络探测，toast 是副作用出口 —— 全部 mock，
// 只验证 connection 自身的状态机（offline/down/ok 迁移 + 轮询 + 幂等停止）。
vi.mock('~/composables/bridge', () => ({
  checkHealth: vi.fn(),
}));
vi.mock('~/composables/toast', () => ({
  registerToastApi: vi.fn(),
  toastInfo: vi.fn(),
  toastSuccess: vi.fn(),
  toastWarn: vi.fn(),
  toastError: vi.fn(),
  sendRequestErrorToast: vi.fn(),
}));

const mockHealth = vi.mocked(checkHealth);
const mockToastInfo = vi.mocked(toastInfo);
const mockToastWarn = vi.mocked(toastWarn);

/** 构造符合 checkHealth 返回类型的健康结果（ narrowed via as，避免 any） */
const health = (healthy: boolean) => ({ healthy }) as Awaited<ReturnType<typeof checkHealth>>;

describe('connection 连通性监控', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockHealth.mockReset();
    _setClientFlag(true);
    // 重置模块级单例私有状态（含决定「恢复 toast」边沿判定的 lastReachable）
    _resetStateForTest();
  });

  afterEach(() => {
    stopConnectionWatch();
    vi.unstubAllGlobals();
    _setClientFlag(false);
    vi.restoreAllMocks();
  });

  it('后端健康 → backendStatus "ok"，且首次成功不弹恢复 toast', async () => {
    mockHealth.mockResolvedValue(health(true));

    await checkConnectivity();

    expect(backendStatus.value).toBe('ok');
    expect(mockToastInfo).not.toHaveBeenCalled();
    expect(mockToastWarn).not.toHaveBeenCalled();
  });

  it('后端不健康（首查即挂）→ "down" + backendDown warn toast', async () => {
    mockHealth.mockResolvedValue(health(false));

    await checkConnectivity();

    expect(backendStatus.value).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledTimes(1);
    // 测试环境无 Nuxt i18n，safeT 原样返回 key（生产为翻译文案）
    expect(mockToastWarn).toHaveBeenCalledWith('connection.backendDown');
  });

  it('checkHealth 抛异常视同后端不可达 → "down" + warn toast', async () => {
    mockHealth.mockRejectedValue(new Error('connection refused'));

    await checkConnectivity();

    expect(backendStatus.value).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledWith('connection.backendDown');
  });

  it('down → ok 恢复时弹 backOnline info toast', async () => {
    mockHealth.mockResolvedValue(health(false));
    await checkConnectivity();
    expect(backendStatus.value).toBe('down');

    mockHealth.mockResolvedValue(health(true));
    await checkConnectivity();

    expect(backendStatus.value).toBe('ok');
    expect(mockToastInfo).toHaveBeenCalledTimes(1);
    expect(mockToastInfo).toHaveBeenCalledWith('connection.backOnline');
  });

  it('navigator.onLine=false → offline warn、backendStatus "down"，且跳过健康检查', async () => {
    vi.stubGlobal('navigator', { onLine: false });

    await checkConnectivity();

    expect(isOnline.value).toBe(false);
    expect(backendStatus.value).toBe('down');
    expect(mockToastWarn).toHaveBeenCalledWith('connection.offline');
    // 浏览器已断网：不浪费一次健康检查
    expect(mockHealth).not.toHaveBeenCalled();
  });

  it('startConnectionWatch 立即首查 + 按 5s 轮询；stop 幂等且不再轮询', async () => {
    vi.useFakeTimers();
    try {
      mockHealth.mockResolvedValue(health(true));

      const stop = startConnectionWatch();
      // 启动即同步发起首次检查
      expect(mockHealth).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(5000);
      expect(mockHealth).toHaveBeenCalledTimes(2);

      await vi.advanceTimersByTimeAsync(5000);
      expect(mockHealth).toHaveBeenCalledTimes(3);

      // 重复启动不叠加定时器（单例轮询）
      const stopAgain = startConnectionWatch();
      expect(vi.getTimerCount()).toBe(1);

      // 停止（含幂等二次调用）后不再轮询
      stopAgain();
      stop();
      await vi.advanceTimersByTimeAsync(15000);
      expect(mockHealth).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('meta.client=false 时 startConnectionWatch 返回空操作句柄、不启动定时器', () => {
    _setClientFlag(false);
    vi.useFakeTimers();
    try {
      const stop = startConnectionWatch();
      expect(typeof stop).toBe('function');
      expect(vi.getTimerCount()).toBe(0);
      expect(() => stop()).not.toThrow();
      expect(mockHealth).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
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
