import { ref, type Ref } from 'vue';
import { checkHealth } from '~/composables/bridge';
import { resolveRuntimeT } from '~/composables/i18nRuntime';
import { toastInfo, toastWarn } from '~/composables/toast';

/**
 * 网络 / 后端连通性监控。
 *
 * 职责：
 *  1. 开启轮询监听：定期调用 bridge.ts 的 `checkHealth()`，把结果反映到响应式
 *     `isOnline` / `backendStatus` 上，供 UI 显示连接状态条。
 *  2. 在状态变化时弹出全局 toast：
 *       - 断网                -> warn  `connection.offline`
 *       - 后端不可达          -> warn  `connection.backendDown`
 *       - 从不可用恢复        -> info  `connection.backOnline`
 *
 * 设计约束：
 *  - 所有导出函数在 `import.meta.client === false` 或未启动监控时都是安全空操作。
 *  - 轮询句柄按需清理，避免泄漏。
 *  - i18n 的 `t` 在非 Nuxt 上下文下安全回退为原样返回 key。
 */

/**
 * 仅测试用：显式覆盖 client 语义标志（生产代码勿调用）。
 * 背景：Vitest 的 import.meta 没有 Nuxt 的 client/server 语义（undefined → falsy），
 * 测试需显式注入。
 * 关键实现约束：生产运行时必须走**字面量** `import.meta.client`——Nuxt/Vite 的构建期
 * 静态替换只作用于该字面量表达式；若经 `const meta = import.meta` 别名访问
 * `meta.client`，别名属性在运行时不存在（undefined → 恒 falsy），客户端守卫会
 * 全部静默失效（2026-08 E2E 实测踩坑：连通性轮询在浏览器全程未启动，横幅永不出现）。
 */
let clientFlagOverride: boolean | null = null;

export function _setClientFlag(client: boolean): void {
  clientFlagOverride = client;
}

/** 当前是否处于浏览器客户端环境（生产走构建期静态替换；测试走显式覆盖）。 */
function isClient(): boolean {
  if (clientFlagOverride !== null) return clientFlagOverride;
  return import.meta.client === true;
}

/** 网络在线状态（浏览器的 navigator.onLine 初值；未得到证实时默认在线）。 */
export const isOnline: Ref<boolean> = ref(true);

/** 后端服务连接状态：'unknown' | 'ok' | 'down'。 */
export type BackendStatus = 'unknown' | 'ok' | 'down';

/** 后端健康状态。 */
export const backendStatus: Ref<BackendStatus> = ref('unknown');

/** 是否正在轮询。 */
const watching = ref(false);

/** 轮询句柄（浏览器环境可能存在）。 */
let pollTimer: ReturnType<typeof setInterval> | null = null;

/** 每次轮询的间隔，单位毫秒。 */
const POLL_INTERVAL_MS = 5000;

/** 上一次观测到的整体可达状态（用于触发「恢复」toast）。 */
let lastReachable: boolean | null = null;

/** i18n key，与 locales/*.json 的 connection.* 对应。 */
const OFF_LINE_KEY = 'connection.offline';
const BACKEND_DOWN_KEY = 'connection.backendDown';
const BACK_ONLINE_KEY = 'connection.backOnline';

/**
 * 安全获取 i18n 翻译。
 *
 * 委托 `resolveRuntimeT()`（i18nRuntime.ts）在 Nuxt 运行时解析真正的翻译函数
 * （nuxt-i18n v10 的 `$i18n` 是不含 t 的 locale 状态代理，不能直接用）；
 * 单测 / 非 Nuxt 上下文回退为原样返回 key。无论哪种情况都不抛出。
 */
function safeT(key: string): string {
  if (!isClient()) return key;
  const t = resolveRuntimeT();
  return t ? t(key) : key;
}

/** 同步一次在线状态（浏览器环境）。 */
function syncBrowserOnline(): void {
  if (!isClient()) return;
  const onLine = typeof navigator !== 'undefined' ? navigator.onLine : true;
  isOnline.value = onLine === true;
}

/**
 * 执行一次连通性检测：结合浏览器在线状态与后端健康检查，更新响应式状态，
 * 并按需弹出「断网 / 后端不可达 / 恢复」toast。
 *
 * 供 startConnectionWatch 的轮询使用，也可由外部手动显式调用（用于初始化）。
 */
export async function checkConnectivity(): Promise<void> {
  if (!isClient()) return;

  syncBrowserOnline();

  // 浏览器已判定断网 -> 直接标记后端不可达并提示
  if (!isOnline.value) {
    backendStatus.value = 'down';
    if (lastReachable !== false) {
      lastReachable = false;
      toastWarn(safeT(OFF_LINE_KEY));
    }
    return;
  }

  // 浏览器在线 -> 尝试健康检查
  let ok = false;
  try {
    const health = await checkHealth();
    ok = health?.healthy === true;
  } catch {
    ok = false;
  }

  backendStatus.value = ok ? 'ok' : 'down';

  if (lastReachable === false) {
    // 之前不可用，现在恢复
    lastReachable = true;
    toastInfo(safeT(BACK_ONLINE_KEY));
  } else if (!ok && lastReachable !== false) {
    // 之前可用（或未知），现在后端不可达
    lastReachable = false;
    toastWarn(safeT(BACKEND_DOWN_KEY));
  } else {
    lastReachable = ok;
  }
}

/**
 * 启动连通性轮询监控。
 *
 * @returns 停止句柄；调用它可停止轮询并清理定时器。
 */
export function startConnectionWatch(): () => void {
  // 非客户端：返回空停止函数，不启动任何轮询
  if (!isClient()) {
    return () => {};
  }

  // 已启动（或正在启动）：返回同一个停止句柄
  if (watching.value) {
    return () => stopConnectionWatch();
  }

  watching.value = true;

  // 立即执行一次，尽早反映当前状态
  void checkConnectivity();

  pollTimer = setInterval(() => {
    void checkConnectivity();
  }, POLL_INTERVAL_MS);

  return () => stopConnectionWatch();
}

/** 停止连通性轮询并清理定时器。幂等。 */
export function stopConnectionWatch(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  watching.value = false;
}

/**
 * 仅测试用：重置模块级单例私有状态（用例隔离，生产代码勿调用）。
 * 关键点是 `lastReachable`——它决定「恢复 toast」的边沿判定，
 * 若在用例间泄漏，前一个用例留下的 down 状态会让下一个用例的失败
 * 被误判为「恢复」。
 */
export function _resetStateForTest(): void {
  isOnline.value = true;
  backendStatus.value = 'unknown';
  lastReachable = null;
  stopConnectionWatch();
}

/**
 * 组合式入口：返回连通性相关的响应式状态与生命周期控制。
 *
 * 供 app.vue / 组件在 setup 中调用；内部复用模块级单例状态，保证全应用共享
 * 同一份 isOnline / backendStatus。
 */
export function useConnection() {
  return {
    isOnline,
    backendStatus,
    startConnectionWatch,
    stopConnectionWatch,
    checkConnectivity,
  };
}
